import Foundation
import Dispatch
import Darwin
import RuntimeCore

package enum ParentEntrypoint {
    package static func runChecked() async { await run(.checked) }
    package static func runCPU() async { await run(.cpu) }
    package static func runBoundaryCPU() async { await run(.boundary) }

    private static func run(_ mode: ParentEntryMode) async {
        _ = signal(SIGPIPE, SIG_IGN)
        let deadline = HostDeadline(origin: DispatchTime.now().uptimeNanoseconds, seconds: 50)
        let evidence = HostEvidence(), roots = ParentEntryRoots()
        // The existing bounded compiler owner is reused for both new entries;
        // it grants no native runtime capability or worker selection.
        let frontends = CPUFrontendHost(deadline: deadline)
        evidence.retain("parent_entry", roots)
        evidence.retain("parent_frontends", frontends)
        var options: ParentHostOptions?, originalReport: JSONValue?
        do {
            let selected = try ParentHostOptions(Array(CommandLine.arguments.dropFirst()), mode: mode)
            options = selected
            try saveHostJSON(.object(["schema": .string("src1b-parent-entry-started-v1"),
                "pid": .integer(Int(getpid())), "pgid": .integer(Int(getpgrp())),
                "host_origin_ns": .string(String(deadline.origin)), "config": selected.diagnostic]),
                to: selected.directory.appendingPathComponent("started.json"))
            let cell = HostResultCell(), gate = HostStartGate()
            let work = Task.detached {
                await gate.wait()
                do {
                    let value: HostWorkResult
                    switch mode {
                    case .checked:
                        value = try await performChecked(selected, deadline: deadline, evidence: evidence,
                            roots: roots, frontends: frontends)
                    case .cpu:
                        value = try await runParentCompositionControls(fixture: selected.fixture!,
                            sourcesDirectory: selected.sourcesDirectory!,
                            evidenceDirectory: selected.directory.appendingPathComponent("parent-controls"),
                            inputsPath: selected.inputs, session: selected.session, vocabularyPath: selected.vocabulary,
                            deadline: deadline, evidence: evidence, frontendHost: frontends, entryRoots: roots)
                    case .boundary:
                        value = try await runParentNativeToolControls(boundaryCase: selected.boundaryCase!,
                            sourcesDirectory: selected.sourcesDirectory!,
                            evidenceDirectory: selected.directory.appendingPathComponent("tool-boundary"),
                            inputsPath: selected.inputs, session: selected.session, vocabularyPath: selected.vocabulary,
                            deadline: deadline, evidence: evidence, frontendHost: frontends, entryRoots: roots)
                    }
                    let closed = await frontends.stopAndJoin()
                    evidence.put("frontend_close", closed)
                    try hostRequire(try closed.field("all_children_reaped").boolean() &&
                        closed.field("all_io_joined").boolean(), "parent compiler child or IO remains unresolved")
                    roots.workReturning()
                    cell.resolve(.work(.success(value)))
                } catch {
                    roots.abort(.runtimeFailure)
                    let original = errorDiagnostic(error)
                    evidence.put("original_work_error", original)
                    do { try await roots.collectAfterFailure() }
                    catch { evidence.put("failure_collection_error", errorDiagnostic(error)) }
                    evidence.put("frontend_failure_close", await frontends.stopAndJoin())
                    roots.workReturning()
                    cell.resolve(.work(.failure(error)))
                }
            }
            roots.installWork(work)
            let timer = Task.detached {
                await gate.wait()
                do { try await Task.sleep(for: .seconds(deadline.remaining)) } catch { return }
                guard cell.reserve() else { return }
                let failure = hostFault("parent-entry-timeout", "fixed 50-second parent entry budget expired")
                roots.abort(.hostDeadline)
                evidence.put("deadline_fault", errorDiagnostic(failure))
                // Publish promptly. Cleanup cannot postpone the primary timeout
                // report; retained original work may still complete afterwards.
                if !cell.publish(.deadline(failure)) { evidence.put("deadline_publish_violation", .string("completion cell refused deadline publication")) }
            }
            roots.installTimer(timer)
            gate.open()
            let completion = await cell.wait()
            let result: HostWorkResult
            let timedOut: Bool
            switch completion {
            case .work(let outcome):
                timedOut = false
                await roots.joinWork()
                switch outcome {
                case .success(let value): result = value
                case .failure(let error):
                    let rejected = error as? FrontendRejection
                    result = HostWorkResult(status: rejected?.status ?? "incomplete",
                        exitCode: rejected?.exitCode ?? 2, value: errorDiagnostic(error))
                }
            case .deadline(let error):
                timedOut = true
                result = HostWorkResult(status: "incomplete", exitCode: 124, value: errorDiagnostic(error))
            }
            await roots.cancelAndJoinTimer()
            let report = try report(options: selected, result: result, evidence: evidence,
                                    deadline: deadline, timedOut: timedOut)
            originalReport = report
            try saveHostJSON(report, to: selected.directory.appendingPathComponent("result.json"))
            if timedOut {
                // The independent OS wrapper bounds this wait. A kill preserves
                // the saved prefix and never becomes an invented task join.
                // If actual late joins arrive, record them without rewriting it.
                await roots.joinWork()
                try saveHostJSON(.object(["schema": .string("src1b-parent-entry-late-v1"),
                    "primary_report": try hostFileIdentity(selected.directory.appendingPathComponent("result.json")),
                    "original_exit_code": .integer(124), "host_roots": evidence.snapshot()]),
                    to: selected.directory.appendingPathComponent("late-entry-closure.json"))
            }
            withExtendedLifetime(evidence) {}
            Darwin.exit(result.exitCode)
        } catch {
            roots.abort(.runtimeFailure)
            let failure: JSONValue = .object(["schema_version": .integer(1),
                "experiment": .string("source-response-parent-host-close-src1b"),
                "status": .string("incomplete"), "exit_code": .integer(2),
                "config": options?.diagnostic ?? .null,
                "diagnostic": errorDiagnostic(error), "host_roots": evidence.snapshot(),
                "preserved_primary_report": originalReport ?? .null, "trials": .array([])])
            if let options {
                let name = originalReport == nil ? "result.json" : "report-error.json"
                do { try saveHostJSON(failure, to: options.directory.appendingPathComponent(name)) }
                catch { try? FileHandle.standardError.write(contentsOf: Data(("cannot save entry failure: \(error)\n").utf8)) }
            }
            try? FileHandle.standardError.write(contentsOf: failure.encoded() + Data([10]))
            withExtendedLifetime(evidence) {}
            Darwin.exit(2)
        }
    }

    private static func performChecked(_ options: ParentHostOptions, deadline: HostDeadline,
        evidence: HostEvidence, roots: ParentEntryRoots, frontends: CPUFrontendHost) async throws -> HostWorkResult {
        func check(_ path: URL, name: String, parent: Bool) async throws -> CheckedProgram {
            let directory = options.directory.appendingPathComponent(name)
            guard Darwin.mkdir(directory.path, mode_t(0o700)) == 0 else {
                throw hostFault("parent-frontend-directory", "exclusive frontend directory failed, errno \(errno)")
            }
            let bridge = CompilerBridge(evidenceDirectory: directory, cpuHost: frontends)
            defer { evidence.put(name, bridge.provenance) }
            let bytes = try readHostSource(path)
            if parent { return try await bridge.checkParentSource(bytes) }
            return try await bridge.checkClosedSource(bytes)
        }
        let parent = try await check(options.parentSource!, name: "parent-frontend", parent: true)
        let child = try await check(options.childSource!, name: "child-frontend", parent: false)
        let budget = try deadline.coreBudget()
        let leaf = try ChildLeafDescriptor(program: child, name: "B", session: options.session,
            outputDirectory: options.directory.appendingPathComponent("child"), watchdogSeconds: budget)
        let vocabulary = try VocabFileCapability(productionPath: options.vocabulary)
        let composition = try ParentCompositionRoot(program: parent, leaf: leaf, inputsPath: options.inputs,
            worker: .mlx, vocabulary: vocabulary, parentWatchdogSeconds: budget, hostBudgetSeconds: budget)
        try roots.install(composition)
        // No source program, child worker, permit reader or registry task has
        // run before both checked programs and this retained root exist.
        let core = try await composition.run()
        evidence.put("parent_primary", core.json)
        try saveHostJSON(core.json, to: options.directory.appendingPathComponent("parent-primary.json"))
        let final = try await composition.joinOriginalHandles()
        evidence.put("parent_final", final)
        try saveHostJSON(final, to: options.directory.appendingPathComponent("parent-closure.json"))
        let closure = try final.field("registry_final_closure")
        let joined = try closure.field("all_created_handles_joined").boolean() && closure.field("observer_joined").boolean()
        let closed = try closure.field("resource_closed").boolean()
        let exitCode: Int32 = (core.exitCode == 0 || core.exitCode == 1) && (!joined || !closed) ? 2 : core.exitCode
        return HostWorkResult(status: exitCode == 0 ? "completed" : exitCode == 1 ? "source-failure" : "incomplete",
            exitCode: exitCode, value: .object([
                "parent_primary": try hostFileIdentity(options.directory.appendingPathComponent("parent-primary.json")),
                "parent_closure": try hostFileIdentity(options.directory.appendingPathComponent("parent-closure.json"))]),
            trials: try core.json.field("trials").elements())
    }

    private static func errorDiagnostic(_ error: any Error) -> JSONValue {
        if let rejection = error as? FrontendRejection { return rejection.diagnostic }
        if let failure = error as? ProtocolError { return failure.diagnostic }
        if let failure = error as? MLXBackendFailure { return failure.raw }
        return .object(["type": .string(String(reflecting: type(of: error))),
                        "description": .string(String(describing: error))])
    }
    private static func report(options: ParentHostOptions, result: HostWorkResult,
        evidence: HostEvidence, deadline: HostDeadline, timedOut: Bool) throws -> JSONValue {
        let binary = (Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])).resolvingSymlinksInPath()
        return .object(["schema_version": .integer(1),
            "experiment": .string("source-response-parent-host-close-src1b"),
            "status": .string(result.status), "exit_code": .integer(Int(result.exitCode)),
            "passed": .bool(result.exitCode == 0), "config": options.diagnostic,
            "provenance": .object(["runtime_binary": try hostFileIdentity(binary),
                "inputs": try hostFileIdentity(URL(fileURLWithPath: options.inputs))]),
            "timing": .object(["host_origin_ns": .string(String(deadline.origin)),
                "snapshot_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
                "host_budget_seconds": .integer(deadline.seconds), "host_deadline_won": .bool(timedOut)]),
            "trials": .array(result.trials), "result": result.value, "host_roots": evidence.snapshot().boundedForReport()])
    }

    package static func runParserCPU() {
        _ = signal(SIGPIPE, SIG_IGN)
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            try hostRequire(args.count == 2 && args[0] == "--output" && args[1].hasPrefix("/"),
                            "parser controls require --output absolute-path")
            let result = runVocabParserNativeControls()
            try saveHostJSON(result, to: URL(fileURLWithPath: args[1]))
            Darwin.exit(try result.field("allPassed").boolean() ? 0 : 1)
        } catch {
            try? FileHandle.standardError.write(contentsOf: errorDiagnostic(error).encoded() + Data([10]))
            Darwin.exit(2)
        }
    }
}
