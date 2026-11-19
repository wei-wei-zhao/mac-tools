//
//  MaccyImport.swift
//  从 Maccy 导入剪贴板历史
//
//  声明：本模块仅读取用户本机已安装的第三方软件 Maccy 所生成的数据文件，
//  未使用、未复制其任何源代码。产品名称仅用于说明数据来源，属于指示性使用。
//  Maccy 是其各自权利人的商标，本项目与 Maccy 官方无任何关联或背书关系。
//
//  Maccy 用 SwiftData（底层 CoreData，存储为 SQLite）：
//    ~/Library/Application Support/Maccy/Storage.sqlite
//
//  核心表结构（CoreData 命名约定）：
//    ZHISTORYITEM         — Z_PK, ZFIRSTCOPIEDAT, ZLASTCOPIEDAT, ZPIN, ZTITLE
//    ZHISTORYITEMCONTENT  — ZTYPE, ZVALUE (BLOB), ZITEM (外键 → ZHISTORYITEM.Z_PK)
//
//  我们只导入纯文本（public.utf8-plain-text），图片等其他类型跳过。
//

import Foundation
import SQLite3

/// SQLite 打开失败的错误
struct MaccyImportError: LocalizedError {
    var message: String
    var errorDescription: String? { message }
}

struct MaccyImportResult {
    var imported: Int = 0
    var skippedNoText: Int = 0
    var skippedDuplicate: Int = 0
    var totalInMaccy: Int = 0
    var errorMessage: String?
}

enum MaccyImporter {

    /// Maccy 数据库位置。也探测旧版路径（Maccy 曾经把文件放在 Containers 里）
    static func databaseURL() -> URL? {
        let fm = FileManager.default
        let candidates: [URL] = [
            fm.homeDirectoryForCurrentUser
                .appending(path: "Library/Application Support/Maccy/Storage.sqlite"),
            fm.homeDirectoryForCurrentUser
                .appending(path: "Library/Containers/org.p0deje.Maccy/Data/Library/Application Support/Maccy/Storage.sqlite"),
        ]
        return candidates.first { fm.fileExists(atPath: $0.path) }
    }

    static func hasMaccyData() -> Bool { databaseURL() != nil }

    /// 读取 Maccy 中的全部文本条目，返回 (文本, 时间, 是否置顶)
    static func readItems() throws -> [(text: String, date: Date, pinned: Bool)] {
        guard let url = databaseURL() else {
            throw MaccyImportError(message: "没有找到 Maccy 数据库")
        }

        var db: OpaquePointer?
        guard sqlite3_open_v2(url.path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            let msg = db.map { String(cString: sqlite3_errmsg($0)) } ?? "未知错误"
            sqlite3_close(db)
            throw MaccyImportError(message: "无法打开 Maccy 数据库：\(msg)")
        }
        defer { sqlite3_close(db) }

        // CoreData 的时间基准是 2001-01-01，和 Date 一致，可直接用
        // 但 ZFIRSTCOPIEDAT 有时为空，回落到 ZLASTCOPIEDAT，再回落到当前时间
        let sql = """
        SELECT
            c.ZVALUE,
            COALESCE(i.ZFIRSTCOPIEDAT, i.ZLASTCOPIEDAT, 0) AS T,
            COALESCE(i.ZPIN, '') AS PIN
        FROM ZHISTORYITEMCONTENT c
        JOIN ZHISTORYITEM i ON c.ZITEM = i.Z_PK
        WHERE c.ZTYPE = 'public.utf8-plain-text'
          AND c.ZVALUE IS NOT NULL
        ORDER BY T DESC
        """

        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            let msg = String(cString: sqlite3_errmsg(db))
            throw MaccyImportError(message: "表结构不匹配（可能是新版 Maccy）：\(msg)")
        }
        defer { sqlite3_finalize(stmt) }

        var out: [(String, Date, Bool)] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            // ZVALUE 是 blob（NSAttributedString 的二进制或纯 UTF-8）
            var text: String?
            if sqlite3_column_type(stmt, 0) == SQLITE_BLOB,
               let bytes = sqlite3_column_blob(stmt, 0) {
                let len = Int(sqlite3_column_bytes(stmt, 0))
                let data = Data(bytes: bytes, count: len)
                text = decodePasteboardText(data)
            } else if let cstr = sqlite3_column_text(stmt, 0) {
                text = String(cString: cstr)
            }

            let t = sqlite3_column_double(stmt, 1)
            let pin = sqlite3_column_text(stmt, 2).map { String(cString: $0) } ?? ""

            guard let body = text, !body.isEmpty else { continue }
            let date = t > 0 ? Date(timeIntervalSinceReferenceDate: t) : Date()
            out.append((body, date, !pin.isEmpty))
        }
        return out
    }

    /// Maccy 存的 "public.utf8-plain-text" 有时是 NSAttributedString 的 RTF/归档二进制，
    /// 有时是裸 UTF-8 字符串。这里依次尝试。
    private static func decodePasteboardText(_ data: Data) -> String? {
        // 1) 裸 UTF-8
        if let s = String(data: data, encoding: .utf8), !s.isEmpty,
           !s.hasPrefix("\u{ef}\u{bb}\u{bf}bplist") {
            // 排除二进制归档：可打印字符占比过低则跳过
            let printable = s.unicodeScalars.filter {
                !CharacterSet.controlCharacters.contains($0) || $0 == "\n" || $0 == "\t"
            }.count
            if printable * 10 >= s.unicodeScalars.count * 8 {
                return s
            }
        }
        // 2) NSAttributedString 归档
        if let attr = try? NSKeyedUnarchiver.unarchivedObject(ofClass: NSAttributedString.self, from: data) {
            let s = attr.string
            return s.isEmpty ? nil : s
        }
        // 3) RTF
        if let attr = NSAttributedString(rtf: data, documentAttributes: nil) {
            let s = attr.string
            return s.isEmpty ? nil : s
        }
        return nil
    }

    /// 执行导入：把 Maccy 的条目合并进当前 store
    @discardableResult
    static func importInto(_ store: ClipStore) -> MaccyImportResult {
        var result = MaccyImportResult()
        do {
            let rows = try readItems()
            result.totalInMaccy = rows.count

            // 现有文本集合，用于去重（O(1)）
            var existing = Set(store.items.map { $0.text })
            var fresh: [ClipItem] = []

            for row in rows {
                if existing.contains(row.text) {
                    result.skippedDuplicate += 1
                    continue
                }
                existing.insert(row.text)
                fresh.append(ClipItem(text: row.text, date: row.date, isPinned: row.pinned))
            }

            if !fresh.isEmpty {
                // 合并后按时间倒序，保持"最新在上"的一致语义
                let merged = (store.items + fresh).sorted { $0.date > $1.date }
                store.replaceAll(with: merged)
            }
            result.imported = fresh.count
        } catch {
            result.errorMessage = error.localizedDescription
        }
        return result
    }
}
