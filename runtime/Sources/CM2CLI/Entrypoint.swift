import Foundation
import Dispatch
import Darwin
import RuntimeCore
import MLXTransport

package enum CM2Entrypoint {
    package static func runChecked() async { await run(.checked) }
    package static func runDeterministic() async { await run(.deterministic) }
    package static func runReference() async { await run(.reference) }
    package static func runCPU() async { await run(.cpu) }
    package static func runExternalCPU() async { await run(.externalCPU) }
    package static func runJoinedDeliveryCPU() async { await run(.joinedDeliveryCPU) }
    package static func runSourceResponseCPU() async { await run(.sourceResponseCPU) }

    private static func run(_ mode: HostMode) async {
        _ = signal(SIGPIPE, SIG_IGN)
        let origin = DispatchTime.now().uptimeNanoseconds
        let deadline = HostDeadline(origin: origin, seconds: mode.isCPU ? 50 : 180)
        let evidence = HostEvidence()
        var options: HostOptions?, context: TransportContext?
        var compiler: CompilerBridge?
        var frontendHost: CPUFrontendHost?
        var originalReport: JSONValue?
        do {
            let chosen = try HostOptions(Array(CommandLine.arguments.dropFirst()), mode: mode)
            options = chosen
            let cpuFrontends = mode.isCPU ? CPUFrontendHost(deadline: deadline) : nil
            frontendHost = cpuFrontends
            if let cpuFrontends { evidence.retain("cpu_frontend", cpuFrontends) }
            let program: CheckedProgram?
            if mode.checksSource {
                let source = try readHostSource(chosen.source!)
                let directory = chosen.directory.appendingPathComponent("frontend")
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
                let bridge = CompilerBridge(evidenceDirectory: directory)
                compiler = bridge
                program = try await bridge.checkClosedSource(source)
                evidence.put("frontend_process", bridge.provenance)
                if let program { try validateExternalDemandConfiguration(program, configured: chosen.externalRequest != nil) }
                if mode == .deterministic, let program {
                    try validateMLXForkDeterministicProgram(program)
                    evidence.put("deterministic_policy_preflight", .object([
                        "validated": .bool(true), "transport_context_created": .bool(false)]))
                }
            } else { program = nil }
            // A rejected public source never creates the permit reader or any
            // worker. These trusted host roots appear only after full checking.
            let transport = try TransportContext(inputsPath: chosen.inputs, session: chosen.session,
                outputDirectory: chosen.directory, returnConversion: chosen.returnConversion,
                returnServicePolicy: chosen.returnService, competitionTasks: chosen.competitionTasks,
                externalRequest: chosen.externalRequest, demandPolicy: chosen.demandPolicy)
            context = transport
            if let demand = transport.externalDemand { evidence.retain("external_demand", ExternalDemandHostRoot(demand)) }
            try transport.record("host.session_started", ["mode": .string(mode.rawValue),
                "swift_pid": .integer(Int(getpid())), "pgid": .integer(Int(getpgrp())),
                "total_budget_seconds": .integer(deadline.seconds)])
            let cell = HostResultCell(), gate = HostStartGate()
            let work = Task.detached {
                await gate.wait()
                do {
                    let value = try await perform(mode: mode, options: chosen, context: transport,
                        evidence: evidence, program: program, deadline: deadline, frontendHost: cpuFrontends)
                    if let demand = transport.externalDemand {
                        if value.status == "completed" { try demand.validateCompletedReply() }
                        else { demand.abort(reason: "host-work-" + value.status) }
                        await demand.finish()
                    }
                    await transport.returnService.finish(reason: "host-work-completed")
                    evidence.returned()
                    cell.resolve(.work(.success(value)))
                } catch {
                    transport.quarantine((error as? MLXBackendFailure) ??
                        hostFault("host_work_failed", String(describing: error)))
                    if let cpuFrontends { evidence.put("frontend_failure_close", await cpuFrontends.stopAndJoin()) }
                    evidence.put("work_error", transport.fault(error))
                    if let demand = transport.externalDemand {
                        demand.abort(reason: "host-work-failed")
                        await demand.finish()
                    }
                    await transport.returnService.finish(reason: "host-work-failed")
                    evidence.returned()
                    cell.resolve(.work(.failure(error)))
                }
            }
            evidence.install(work)
            try transport.record("host.top_level_work_registered", ["roots": evidence.snapshot()])
            let timer = Task.detached {
                do { try await Task.sleep(for: .seconds(deadline.remaining)) } catch { return }
                guard cell.reserve() else { return }
                let failure = hostFault("swift_total_timeout", "fixed \(deadline.seconds)-second entry budget expired")
                // Claim -> quarantine -> expose. No original producer is
                // cancelled, and a timeout never authorizes RELEASE.
                transport.quarantine(failure)
                transport.abortReturnServiceControls(reason: "host-deadline")
                evidence.put("deadline_fault", transport.fault(failure))
                if let cpuFrontends { evidence.put("frontend_deadline_close", await cpuFrontends.stopAndJoin()) }
                cell.publish(.deadline(failure))
            }
            transport.externalDemand?.prepare()
            gate.open()
            let completion = await cell.wait()
            let result: HostWorkResult
            switch completion {
            case .work(let outcome):
                await evidence.join()
                try transport.record("host.top_level_work_join_returned")
                switch outcome {
                case .success(let value): result = value
                case .failure(let error):
                    result = HostWorkResult(status: "incomplete", exitCode: 2,
                        value: .object(["fault": transport.fault(error)]))
                }
            case .deadline(let failure):
                // The exact work handle stays in HostEvidence. Do not wait
                // indefinitely or describe cancellation as an actual join.
                result = HostWorkResult(status: "incomplete", exitCode: 124,
                    value: .object(["fault": transport.fault(failure)]))
            }
            timer.cancel()
            await timer.value
            try transport.record("host.timer_join_returned")
            // The original work/timer and successful worker/IO close have
            // completed. This is a collection boundary, not a join of all old
            // Dispatch deadline callbacks. Incomplete snapshots are prefixes.
            let serviceComplete = result.status == "completed" || result.status == "source-failure" || result.status == "cpu-controls-failed"
            evidence.put("service_observation", transport.saveServiceObservation(complete: serviceComplete))
            evidence.put("return_conversion_observation", transport.saveReturnConversionObservation(complete: serviceComplete))
            evidence.put("return_service_observation", transport.saveReturnServiceObservation(complete: serviceComplete))
            evidence.put("return_service_abort_root", transport.returnServiceAbortSnapshot(complete: serviceComplete))
            evidence.put("external_demand_observation", transport.saveExternalDemandObservation(complete: serviceComplete))
            let report = try makeReport(options: chosen, status: result.status, exitCode: result.exitCode,
                value: result.value, trials: result.trials, context: transport, evidence: evidence,
                frontend: compiler?.provenance ?? .null, deadline: deadline)
            originalReport = report
            try saveHostJSON(report, to: chosen.directory.appendingPathComponent("result.json"))
            try transport.record("host.result_saved", ["status": .string(result.status),
                "exit_code": .integer(Int(result.exitCode)), "file": .string("result.json")])
            await transport.flush()
            withExtendedLifetime(evidence) {}
            Darwin.exit(result.exitCode)
        } catch {
            context?.quarantine((error as? MLXBackendFailure) ??
                hostFault("host_entry_failed", String(describing: error)))
            if let frontendHost { evidence.put("frontend_entry_failure_close", await frontendHost.stopAndJoin()) }
            if let context { evidence.put("service_observation", context.saveServiceObservation(complete: false)) }
            if let context { evidence.put("return_conversion_observation", context.saveReturnConversionObservation(complete: false)) }
            if let context {
                context.abortReturnServiceControls(reason: "entry-failure")
                evidence.put("return_service_observation", context.saveReturnServiceObservation(complete: false))
                evidence.put("return_service_abort_root", context.returnServiceAbortSnapshot(complete: false))
                evidence.put("external_demand_observation", context.saveExternalDemandObservation(complete: false))
            }
            let status: String, code: Int32, diagnostic: JSONValue
            if let rejected = error as? FrontendRejection {
                status = rejected.status; code = rejected.exitCode; diagnostic = rejected.diagnostic
            } else if let failure = error as? ProtocolError {
                status = "incomplete"; code = 2; diagnostic = failure.diagnostic
            } else {
                status = "incomplete"; code = 2
                diagnostic = context?.fault(error) ?? .object(["message": .string(String(describing: error))])
            }
            let failure: JSONValue = .object(["schema_version": .integer(1),
                "experiment": .string("core-language-runtime-mlx-fork-cm2"), "mode": .string(mode.rawValue),
                "status": .string(status), "exit_code": .integer(Int(code)), "diagnostic": diagnostic,
                "provenance": .object(["frontend_process": compiler?.provenance ?? .null]),
                "config": .object(["total_budget_seconds": .integer(deadline.seconds),
                    "return_conversion": options.map { .string($0.returnConversion.rawValue) } ?? .null,
                    "return_service": options.map { .string($0.returnService.rawValue) } ?? .null,
                    "competition_tasks": options.map { .integer($0.competitionTasks) } ?? .null,
                    "external_request": options?.externalRequest?.json ?? .null,
                    "demand_policy": options?.demandPolicy.map { .string($0.rawValue) } ?? .null,
                    "external_result_delivery": options?.externalRequest == nil ? .null : options.map { .string($0.externalResultDelivery.rawValue) } ?? .null,
                    "delivery_fixture": options?.deliveryFixture.map(JSONValue.string) ?? .null,
                    "response_fixture": options?.responseFixture.map(JSONValue.string) ?? .null]), "trials": .array([]),
                "transport_context_created": .bool(context != nil), "transport_roots": context?.snapshot() ?? .null,
                "host_roots": evidence.snapshot(), "preserved_report": originalReport ?? .null])
            if let options {
                let name = originalReport == nil ? "result.json" : "report-error.json"
                do { try saveHostJSON(failure, to: options.directory.appendingPathComponent(name)) }
                catch { try? FileHandle.standardError.write(contentsOf: Data(("failed saving original diagnostic: \(error)\n").utf8)) }
            }
            try? FileHandle.standardError.write(contentsOf: failure.encoded() + Data([10]))
            if let context { await context.flush() }
            withExtendedLifetime(evidence) {}
            Darwin.exit(code)
        }
    }

    private static func perform(mode: HostMode, options: HostOptions, context: TransportContext,
                                evidence: HostEvidence, program: CheckedProgram?, deadline: HostDeadline,
                                frontendHost: CPUFrontendHost?) async throws -> HostWorkResult {
        switch mode {
        case .checked, .deterministic:
            guard let program else { throw hostFault("missing_checked_source", "public entry lost its validated source") }
            let driver = try await context.startChecked()
            let budget = try deadline.coreBudget()
            let core: ExecutionReport
            if mode == .deterministic {
                core = await executeMLXForkDeterministic(program, backend: driver, hostBudgetSeconds: budget, returnService: context.returnService)
            } else {
                core = await execute(program, backend: driver, hostBudgetSeconds: budget, returnService: context.returnService,
                                     externalDemand: context.externalDemand, externalResultDelivery: options.externalResultDelivery)
            }
            evidence.put("core_report", core.json)
            try saveHostJSON(core.json, to: options.directory.appendingPathComponent("core.json"))
            let trials = try core.json.members()["trials"]?.elements() ?? []
            if try core.json.field("status").text() == "completed" {
                try hostRequire(core.exitCode == 0 || core.exitCode == 1, "completed Core has an incompatible exit")
                try hostRequire(trials.count == 1, "completed Core lost its original trial")
                let ledger = try trials[0].field("ledger")
                try hostRequire(try ledger.field("host_main_joined").boolean() && ledger.field("backend_closed").boolean() &&
                    ledger.field("host_join_observer").field("actual_joined").boolean(), "Core returned without full production close/join")
                return HostWorkResult(status: core.exitCode == 0 ? "completed" : "source-failure", exitCode: core.exitCode,
                    value: .object(["core_report": try hostFileIdentity(options.directory.appendingPathComponent("core.json"))]), trials: trials)
            }
            context.quarantine(hostFault("core_incomplete", "Core retained unresolved source/backend roots; no host release is authorized"))
            return HostWorkResult(status: "incomplete", exitCode: core.exitCode == 0 ? 2 : core.exitCode,
                value: .object(["core_report": try hostFileIdentity(options.directory.appendingPathComponent("core.json"))]), trials: trials)
        case .reference:
            return try await CM2CLI.runReference(context: context, evidence: evidence, directory: options.directory)
        case .sourceResponseCPU:
            guard let frontendHost, let fixture = options.responseFixture else {
                throw hostFault("missing_response_cpu_owner", "source response CPU frontend/fixture was not installed")
            }
            let controlsDirectory = options.directory.appendingPathComponent("source-response-controls")
            let controls = try await runSourceResponseControls(fixture: fixture,
                sourcesDirectory: options.sourcesDirectory!, evidenceDirectory: controlsDirectory,
                context: context, frontendHost: frontendHost, evidence: evidence)
            evidence.put("source_response_controls", controls)
            let frontendClosed = await frontendHost.stopAndJoin()
            evidence.put("cpu_frontend_close", frontendClosed)
            try hostRequire(try frontendClosed.field("sealed").boolean() &&
                frontendClosed.field("all_children_reaped").boolean() && frontendClosed.field("all_io_joined").boolean(),
                "source response CPU frontend child or IO remains unresolved")
            let finalRoots = try await context.finish()
            let passed = try controls.field("passed").boolean()
            return HostWorkResult(status: passed ? "completed" : "cpu-controls-failed", exitCode: passed ? 0 : 1,
                value: .object(["controls": controls, "controls_directory": .string(controlsDirectory.path),
                    "frontend_close": frontendClosed, "final_transport_roots": finalRoots]))
        case .joinedDeliveryCPU:
            guard let frontendHost, let fixture = options.deliveryFixture else {
                throw hostFault("missing_delivery_cpu_owner", "joined delivery CPU frontend/fixture was not installed")
            }
            let controlsDirectory = options.directory.appendingPathComponent("joined-delivery-controls")
            let controls = try await runJoinedResultDeliveryControls(fixture: fixture,
                sourcesDirectory: options.sourcesDirectory!, evidenceDirectory: controlsDirectory,
                context: context, frontendHost: frontendHost, evidence: evidence)
            evidence.put("joined_result_delivery_controls", controls)
            let frontendClosed = await frontendHost.stopAndJoin()
            evidence.put("cpu_frontend_close", frontendClosed)
            try hostRequire(try frontendClosed.field("sealed").boolean() &&
                frontendClosed.field("all_children_reaped").boolean() && frontendClosed.field("all_io_joined").boolean(),
                "joined delivery CPU frontend child or IO remains unresolved")
            let finalRoots = try await context.finish()
            let passed = try controls.field("passed").boolean()
            return HostWorkResult(status: passed ? "completed" : "cpu-controls-failed", exitCode: passed ? 0 : 1,
                value: .object(["controls": controls, "controls_directory": .string(controlsDirectory.path),
                    "frontend_close": frontendClosed, "final_transport_roots": finalRoots]))
        case .externalCPU:
            guard let frontendHost else { throw hostFault("missing_cpu_frontend_owner", "external CPU compiler owner was not installed") }
            let controlsDirectory = options.directory.appendingPathComponent("external-source-controls")
            // The callee exclusively creates this leaf and saves every original
            // Core/worker prefix. No old fixtures or native microcontrols run here.
            let controls = try await runExternalDemandSourceControls(sourcesDirectory: options.sourcesDirectory!,
                evidenceDirectory: controlsDirectory, context: context, frontendHost: frontendHost, evidence: evidence)
            evidence.put("external_demand_source_controls", controls)
            let frontendClosed = await frontendHost.stopAndJoin()
            evidence.put("cpu_frontend_close", frontendClosed)
            try hostRequire(try frontendClosed.field("sealed").boolean() &&
                frontendClosed.field("all_children_reaped").boolean() && frontendClosed.field("all_io_joined").boolean(),
                "external CPU frontend direct children or IO remain unresolved")
            let finalRoots = try await context.finish()
            let passed = try controls.field("passed").boolean()
            return HostWorkResult(status: passed ? "completed" : "cpu-controls-failed", exitCode: passed ? 0 : 1,
                value: .object(["controls": controls, "controls_directory": .string(controlsDirectory.path),
                    "frontend_close": frontendClosed, "final_transport_roots": finalRoots]))
        case .cpu:
            guard let frontendHost else { throw hostFault("missing_cpu_frontend_owner", "CPU compiler owner was not installed") }
            let retainedAbortControl = try context.installReturnServiceAbortControl()
            let abortControl = await runRSPAbortControl(retainedAbortControl)
            try saveHostJSON(abortControl, to: options.directory.appendingPathComponent("return-service-abort-control.json"))
            evidence.put("return_service_abort_control", abortControl)
            try hostRequire(abortControl.brsField("passed") == .bool(true), "RSP1 native abort control")
            let memory = await runBRSServiceMemoryControls()
            try saveHostJSON(memory, to: options.directory.appendingPathComponent("service-memory-controls.json"))
            evidence.put("service_memory_controls", memory)
            try hostRequire(memory.brsField("passed") == .bool(true), "BRS1 shared memory/Promise controls")
            let controls = try await runCPUControls(sourcesDirectory: options.sourcesDirectory!,
                evidenceDirectory: options.directory, context: context, frontendHost: frontendHost)
            evidence.put("cpu_controls", controls)
            let frontendClosed = await frontendHost.stopAndJoin()
            evidence.put("cpu_frontend_close", frontendClosed)
            try hostRequire(try frontendClosed.field("sealed").boolean() &&
                frontendClosed.field("all_children_reaped").boolean() && frontendClosed.field("all_io_joined").boolean(),
                "CPU frontend direct children or IO remain unresolved")
            let finalRoots = try await context.finish()
            let registered = brsRegisteredCPUControl(context.serviceObservation.snapshot(complete: true))
            try saveHostJSON(registered, to: options.directory.appendingPathComponent("service-registered-control.json"))
            evidence.put("service_registered_control", registered)
            let passed = try controls.field("passed").boolean() && registered.brsField("passed") == .bool(true)
            return HostWorkResult(status: passed ? "completed" : "cpu-controls-failed", exitCode: passed ? 0 : 1,
                value: .object(["controls": controls, "frontend_close": frontendClosed, "final_transport_roots": finalRoots]))
        }
    }

    private static func makeReport(options: HostOptions, status: String, exitCode: Int32,
                                   value: JSONValue, trials: [JSONValue], context: TransportContext,
                                   evidence: HostEvidence, frontend: JSONValue, deadline: HostDeadline) throws -> JSONValue {
        let binary = (Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])).resolvingSymlinksInPath()
        return .object(["schema_version": .integer(1), "experiment": .string("core-language-runtime-mlx-fork-cm2"),
            "mode": .string(options.mode.rawValue), "session": .string(options.session),
            "status": .string(status), "exit_code": .integer(Int(exitCode)), "passed": .bool(exitCode == 0),
            "provenance": .object(["runtime_binary": try hostFileIdentity(binary),
                "frontend_process": frontend, "input_manifest": try hostFileIdentity(URL(fileURLWithPath: options.inputs)),
                "backend_inputs": context.inputProvenance]),
            "config": .object(["total_budget_seconds": .integer(deadline.seconds),
                "return_conversion": .string(options.returnConversion.rawValue),
                "return_service": .string(options.returnService.rawValue),
                "competition_tasks": .integer(options.competitionTasks),
                "external_request": options.externalRequest?.json ?? .null,
                "demand_policy": options.demandPolicy.map { .string($0.rawValue) } ?? .null,
                "external_result_delivery": options.externalRequest == nil ? .null : .string(options.externalResultDelivery.rawValue),
                "delivery_fixture": options.deliveryFixture.map(JSONValue.string) ?? .null,
                "response_fixture": options.responseFixture.map(JSONValue.string) ?? .null,
                "source_path": options.source.map { .string($0.path) } ?? .null,
                "sources_directory": options.sourcesDirectory.map { .string($0.path) } ?? .null]),
            "trials": .array(trials), "result": value, "host_roots": evidence.snapshot(),
            "transport_roots": context.snapshot(), "transport_events": context.events()])
    }
}
