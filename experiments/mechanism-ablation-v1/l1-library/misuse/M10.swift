import Foundation

// MA1 M10 compile negative (level L1 / dynamic-ledger library).
//
// Catalog M10: "a non-creator cancels the producer; the consumer handle has no
// cancel method." This file documents what the L1 library actually enforces at
// COMPILE time, as opposed to the P4 capability-split experiment the catalog
// row was originally written against.
//
// Finding, in two parts:
//  (1) The library exposes NO producer-cancel API. The call below references a
//      method that does not exist; `swiftc -typecheck` rejects it. That is the
//      only compile-time guarantee here.
//  (2) But there is NO access-control barrier: OriginalRun.task is an in-module
//      `let`, so same-module code can call run.task.cancel() directly and it
//      compiles. The capability (who may cancel the producer) is not expressed
//      in the type system. Only the producer being unexposed as an API, plus
//      the Owner never calling cancel on it, keeps it safe at L1.
func m10_nonexistent_producer_cancel(_ run: OriginalRun, _ owner: KVLoopOwner) {
    // (1) No such API -> compile error (this is the negative we capture):
    run.cancelProducer()
    owner.cancelProducer()
}

// (2) Enable to confirm the direct producer-Task cancel COMPILES (no barrier).
// Left commented so the file's only error is the missing-API one above.
// func m10_direct_task_cancel_compiles(_ run: OriginalRun, _ ui: UIRoot) {
//     run.task.cancel()   // compiles: no capability control in-module
//     ui.cancel()         // compiles: any holder may cancel the waiter
// }
