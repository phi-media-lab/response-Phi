import Foundation
import RuntimeCore

/// File IO and strict validation finish before Context, workers or requester
/// Tasks exist. The exact bytes are pinned in the immutable request record.
func readExternalDemandRequest(_ path: String, session: String) throws -> ExternalDemandRequest {
    try hostRequire(path.hasPrefix("/"), "external request path must be absolute")
    let url = URL(fileURLWithPath: path).standardizedFileURL
    let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
    try hostRequire(attributes[.type] as? FileAttributeType == .typeRegular, "external request must be a regular file")
    let handle = try FileHandle(forReadingFrom: url)
    defer { try? handle.close() }
    var bytes = Data()
    while bytes.count <= ExternalDemandRequest.byteLimit {
        let count = min(16_384, ExternalDemandRequest.byteLimit + 1 - bytes.count)
        guard let chunk = try handle.read(upToCount: count), !chunk.isEmpty else { break }
        bytes.append(chunk)
    }
    return try ExternalDemandRequest(data: bytes, path: url.path, expectedSession: session)
}

/// Evidence retains this wrapper before Control.prepare creates the actual
/// requester handle. Snapshot JSON is never the retained lifecycle authority.
final class ExternalDemandHostRoot: HostRetainedRoot, Sendable {
    let control: ExternalDemandControl
    init(_ control: ExternalDemandControl) { self.control = control }
    func snapshot() -> JSONValue { control.snapshot(complete: false) }
}
