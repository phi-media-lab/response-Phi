import Foundation

/// Historical evidence only. This adapter creates no Ledger, Task, resource,
/// backend driver or Session. The original validators remain in MLXEvidence.
package struct BRCPreparedCoreCase: Sendable {
    package let receiptCore: JSONValue
    package let evidence: JSONValue
    private let expected: MLXReturnRequest
    private let ack: MLXReturnACK

    package init(ready: JSONValue, receipt: JSONValue, acknowledgement: JSONValue,
                 ledger: JSONValue) throws {
        let source = try receipt.field("source_context")
        let sourceSession = try source.field("session_identity")
        let poolRows = try ready.field("pools").elements()
        let pools = try poolRows.map { row in
            MLXPoolIdentity(layer: try Self.int(row.field("layer")),
                poolID: try row.field("pool_id").text(),
                objectID: try row.field("pool_object_id").text())
        }
        let session = try MLXSessionIdentity(session: ready.field("session").text(),
            worker: ready.field("worker_id").text(), pid: Self.int(ready.field("pid")),
            pgid: Self.int(ready.field("pgid")),
            poolGroup: sourceSession.field("pool_group").text(), pools: pools,
            provenance: ready) // initializedIdentity uses the ENTIRE original READY.
        try runtimeRequire(session.json == sourceSession, "BRC complete original session identity")

        let job = try source.field("job").text()
        let callerRaw = try ledger.field("J").field(job).field("caller")
        _ = try callerRaw.members(exactly: ["job", "task", "invocation", "region", "scopes"])
        let caller = try Caller(job: callerRaw.field("job").text(),
            task: callerRaw.field("task").text(), invocation: callerRaw.field("invocation").text(),
            region: callerRaw.field("region").text(),
            scopes: callerRaw.field("scopes").elements().map { try $0.text() })
        try runtimeRequire(caller.json == callerRaw && caller.job == job, "BRC original five-field Caller")
        let request = try MLXEvalRequest(commandID: source.field("command").text(),
            operation: source.field("operation").text(), tokens: backendInts(source.field("tokens")),
            owner: source.field("owner").text(), sourceEpoch: source.field("source_epoch").text(),
            ownerOrdinal: Self.int(source.field("owner_ordinal")), scope: source.field("scope").text(),
            returnID: source.field("backend_return").text(), caller: caller,
            access: source.field("access").text(), site: source.field("site").text(), session: session)
        try runtimeRequire(request.json == source, "BRC complete original source request")

        let original = MLXHostReceipt(request: request, raw: receipt)
        let returnRequest = MLXReturnRequest(receipt: original, returnID: request.returnID, scope: request.scope)
        let returnACK = MLXReturnACK(request: returnRequest, raw: acknowledgement)
        let record = try ledger.field("backend_returns").field(request.returnID)
        try runtimeRequire(record.field("id").text() == request.returnID &&
            record.field("phase").text() == "acknowledged" &&
            record.field("receipt") == original.json && record.field("ack") == returnACK.json,
            "BRC original Core backend-return receipt/ACK correspondence")
        try runtimeRequire(record.field("command").text() == request.commandID &&
            record.field("scope").text() == request.scope && record.field("owner").text() == request.owner &&
            record.field("source_epoch").text() == request.sourceEpoch &&
            record.field("pool_group").text() == session.poolGroup,
            "BRC original Core backend-return identity")
        let command = try ledger.field("mlx_commands").field(request.commandID)
        try runtimeRequire(command.field("request") == request.json && command.field("receipt") == original.json,
            "BRC original Core command evidence")
        self.receiptCore = receipt
        self.expected = returnRequest
        self.ack = returnACK
        self.evidence = .object([
            "caller": caller.json, "session_identity": session.json, "request": request.json,
            "receipt": original.json, "expected": returnRequest.json, "ack": returnACK.json,
            "original_backend_return": record, "original_command": command,
            "checks": .object(["full_request_equal": .bool(true), "five_field_caller_equal": .bool(true),
                "full_receipt_equal": .bool(true), "full_ack_equal": .bool(true),
                "ready_is_entire_provenance": .bool(true)])])
    }

    /// The measured call is the unchanged production Core validator.
    package func validateReturn() throws { try validateMLXReturn(ack, expected: expected) }

    /// Used only by the pre-measurement negative control: expected stays fixed.
    package func validateReturn(raw: JSONValue) throws {
        try validateMLXReturn(MLXReturnACK(request: expected, raw: raw), expected: expected)
    }

    package static func isOriginalIdentityError(_ error: any Error) -> Bool {
        guard let original = error as? RuntimeInvariant else { return false }
        return original.description == "release acknowledgement identity"
    }

    private static func int(_ value: JSONValue) throws -> Int {
        let token = try value.integerToken()
        guard let result = Int(token) else { throw ExactJSONError("BRC integer does not fit Int") }
        return result
    }
}
