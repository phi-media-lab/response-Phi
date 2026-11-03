import Foundation
import RuntimeCore

/// CPU-only controls call the actual Promise and leaf sink. No new Task,
/// observer, callback, worker, or scheduling gate is introduced. Registered
/// delivery is checked separately from the existing CPU fixtures' real path.
package func runBRSServiceMemoryControls() async -> JSONValue {
    let cached = BRSServiceTrace(session: "cpu-memory-cached", role: "test-cached")
    let observed = BRSObservedPromise(sink: cached, key: .object(["test": .string("cached")]),
        origin: .null, promiseID: "test/cached")
    let promise = TransportPromise<Int>(observation: observed)
    let first = promise.resolve(.success(17))
    var value: JSONValue = .null, failure: JSONValue = .null
    do { value = .integer(try await promise.wait()) }
    catch { failure = .string(String(describing: error)) }
    let repeated = promise.resolve(.success(99))
    let cachedRaw = cached.snapshot(complete: true)
    let rows = (try? cachedRaw.brsField("events").elements()) ?? []
    let names = rows.map { $0.brsField("event") }
    let expected = ["wait.cached", "cached.inline.pre", "cached.inline.post", "wait.return"].map(JSONValue.string)
    let waits = Set(rows.compactMap { try? $0.brsField("details").brsField("wait_instance").text() })

    // This independent, same-class instance cannot overflow the session sink.
    // Reverse append two real captures; append order is not capture order.
    let bounded = BRSServiceTrace(session: "cpu-memory-bounded", role: "test-overflow", capacity: 2)
    let earlier = BRSServiceStamp.capture(domain: "diagnostic")
    let later = BRSServiceStamp.capture(domain: "diagnostic")
    bounded.record("test.later", domain: "diagnostic", stamp: later)
    bounded.record("test.earlier", domain: "diagnostic", stamp: earlier)
    bounded.record("test.dropped", domain: "diagnostic")
    let boundedRaw = bounded.snapshot(complete: true)
    let boundedRows = (try? boundedRaw.brsField("events").elements()) ?? []
    let nullPriority = boundedRows.allSatisfy {
        $0.brsField("priority") == .null && $0.brsField("priority_null_reason") == .string("non-task-domain")
    }
    let checks: [(String, Bool)] = [
        ("original-cached-result-once", first && !repeated && value == .integer(17) && failure == .null),
        ("cached-inline-native-path", names.count == expected.count && expected.allSatisfy { name in names.filter { $0 == name }.count == 1 } && waits.count == 1),
        ("bounded-independent-instance", boundedRows.count == 2 && boundedRaw.brsField("dropped") == .integer(1)
            && boundedRaw.brsField("measurement_valid") == .bool(false)),
        ("capture-and-append-distinct", earlier.captureNS <= later.captureNS &&
            boundedRows.first?.brsField("event") == .string("test.later") &&
            boundedRows.last?.brsField("event") == .string("test.earlier")),
        ("diagnostic-domain-null-priority", nullPriority)]
    return .object(["schema": .string("brs1-memory-controls-v1"),
        "passed": .bool(checks.allSatisfy { $0.1 }),
        "checks": .array(checks.map { .object(["name": .string($0.0), "passed": .bool($0.1)]) }),
        "original_value": value, "original_error": failure,
        "cached": cachedRaw, "bounded": boundedRaw,
        "registered_control": .string("existing-CPU-registered-path; no new micro-waiter")])
}

/// Pure inspection of the production sink after the original CPU suite/IO
/// close. The independent auditor repeats this from raw, not from `passed`.
package func brsRegisteredCPUControl(_ snapshot: JSONValue) -> JSONValue {
    let rows = (try? snapshot.brsField("events").elements()) ?? []
    let registered = rows.filter { $0.brsField("event") == .string("wait.registered") }
    let groups: [JSONValue] = registered.map { start in
        let wait = start.brsField("details").brsField("wait_instance")
        let promise = start.brsField("details").brsField("promise_id")
        let group = rows.filter {
            $0.brsField("details").brsField("wait_instance") == wait &&
            $0.brsField("details").brsField("promise_id") == promise
        }
        func named(_ name: String) -> [JSONValue] { group.filter { $0.brsField("event") == .string(name) } }
        let pre = named("resume.pre"), post = named("resume.post"), returned = named("wait.return")
        let once = named("wait.registered").count == 1 && pre.count == 1 && post.count == 1 && returned.count == 1 &&
            named("wait.cached").isEmpty && named("cached.inline.pre").isEmpty && named("cached.inline.post").isEmpty
        let result = pre.first?.brsField("details").brsField("result") ?? .null
        let resultsMatch = (result == .string("success") || result == .string("failure")) &&
            post.first?.brsField("details").brsField("result") == result &&
            returned.first?.brsField("details").brsField("result") == result
        return .object(["wait_instance": wait, "promise_id": promise, "passed": .bool(once && resultsMatch),
            "events": .array(group)])
    }
    let passed = !groups.isEmpty && groups.allSatisfy { $0.brsField("passed") == .bool(true) } &&
        snapshot.brsField("measurement_valid") == .bool(true)
    return .object(["schema": .string("brs1-existing-registered-control-v1"),
        "source": .string("original 8 CPU worker fixtures; EVAL/RELEASE production Promise"),
        "physical_suspend_claimed": .bool(false), "passed": .bool(passed),
        "registered_count": .integer(groups.count), "groups": .array(groups)])
}
