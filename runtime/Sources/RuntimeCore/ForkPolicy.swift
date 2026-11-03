import Foundation

/// Trusted native controls only. Source/checked artifacts cannot choose these.
package enum MLXForkTestPolicy: String, Sendable {
    case none, ownedCoexist, attachedCancellation, earlyCancel, closedTarget
}

/// Implemented only by the CPU transport driver. The first method observes a
/// validated actual EVAL_ENTER; the second sends a request-bound pipe permit.
package protocol MLXForkCPUPhaseControl: Sendable {
    func waitForEvaluationEntered(job:String) async throws -> JSONValue
    func continueEvaluation(job:String) async throws -> JSONValue
}

struct ForkDeterministicBinding:Sendable {
    let sourceSHA256:String,sourceBytesSHA256:String,producerSite:String,forkSite:String,waitSite:String,scopeSite:String
}

/// First accepted F13 bytes/sites (static-root-acceptance-v1.json). This is a
/// frozen native constant, never derived from the current execution input.
private let cm2DeterministicBinding:ForkDeterministicBinding? = .init(
    sourceSHA256:"1f90cbe0b695da248f4acd2e214229fe796dad044731ba9fa4c8cab7b05ef41a",
    sourceBytesSHA256:"ea4b1f48fb357570d643c3900e80a80e2d778f2746e981d6a1f65b2058d1e597",
    producerSite:"/main/body/body/first/body",
    forkSite:"/main/body/body/first/body/next/first/body",
    waitSite:"/main/body/body/first/body/next/first/body/job/body/first",
    scopeSite:"/main/body/body/first/body/next/first")

package func validateMLXForkDeterministicProgram(_ program:CheckedProgram)throws {
    _ = try deterministicBinding(program)
}
func deterministicBinding(_ program:CheckedProgram)throws->ForkDeterministicBinding {
    guard let binding=cm2DeterministicBinding else {
        throw RuntimeInvariant("CM2 deterministic policy is unbound; accepted F13 source hash/sites must be frozen before execution")
    }
    try runtimeRequire(program.sourceHash == binding.sourceSHA256 && program.rawHash == binding.sourceBytesSHA256,"CM2 deterministic source hash mismatch")
    for (site,command) in [(binding.producerSite,"produce"),(binding.forkSite,"fork"),(binding.waitSite,"await_cancel"),(binding.scopeSite,"scope")] {
        try runtimeRequire(try program.nodes[site]?.field("command").text() == command,"CM2 deterministic source site mismatch")
    }
    return binding
}

/// Holds the original Task's native continuation (and thus indirectly its
/// stack), never a duplicate source environment, Machine, or restorable holder.
/// The owning original Task awaits this gate. Timeout never releases it.
final class ForkMilestone:@unchecked Sendable {
    private let lock=NSLock()
    private var opened=false
    private var waiters:[CheckedContinuation<Void,Never>]=[]
    func wait()async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if opened {lock.unlock();continuation.resume()}
            else {waiters.append(continuation);lock.unlock()}
        }
    }
    func release() {
        lock.lock()
        guard !opened else {lock.unlock();return}
        opened=true;let pending=waiters;waiters.removeAll();lock.unlock()
        for waiter in pending {waiter.resume()}
    }
    var json:JSONValue {
        lock.lock();defer{lock.unlock()}
        return .object(["open":.bool(opened),"native_waiters":.integer(waiters.count),"duplicate_source_holder_retained":.bool(false)])
    }
}
