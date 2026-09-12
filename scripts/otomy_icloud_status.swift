import Foundation

// Report upload confirmation only; never read or print financial contents.
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let keys: Set<URLResourceKey> = [.isRegularFileKey, .isUbiquitousItemKey,
    .ubiquitousItemIsUploadedKey, .ubiquitousItemUploadingErrorKey]
var total = 0, uploaded = 0, errors = 0, unknown = 0
if let files = FileManager.default.enumerator(at: root, includingPropertiesForKeys: Array(keys), options: []) {
    for case let file as URL in files {
        do {
            let v = try file.resourceValues(forKeys: keys)
            if v.isRegularFile != true { continue }
            total += 1
            if v.ubiquitousItemUploadingError != nil { errors += 1 }
            if v.isUbiquitousItem == true && v.ubiquitousItemIsUploaded == true { uploaded += 1 }
            else { unknown += 1 }
        } catch { errors += 1 }
    }
}
let state = total > 0 && uploaded == total && errors == 0 ? "uploaded" : "unconfirmed"
let data = try JSONSerialization.data(withJSONObject: ["state": state, "files": total,
    "uploaded": uploaded, "errors": errors, "unconfirmed": unknown], options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
