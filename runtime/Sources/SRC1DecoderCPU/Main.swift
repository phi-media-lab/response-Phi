import Foundation
import CryptoKit
import Darwin
import RuntimeCore

// Synchronous production-decoder observations only. No Task, Ledger, compiler
// child, transport, worker or model is created. Root supplies actual exports.
private struct ProbeFailure: Error { let message:String }
private func require(_ condition:Bool,_ message:String)throws {
    if !condition {throw ProbeFailure(message:message)}
}
private func sha(_ data:Data)->String {SHA256.hash(data:data).map{String(format:"%02x",$0)}.joined()}
private func expectedDiagnostic(_ message:String)->JSONValue {
    .object(["phase":.string("protocol"),"code":.string("invalid-compiler-exchange"),
             "path":.array([]),"message":.string(message)])
}
private func pin(_ path:URL,_ data:Data)->JSONValue {
    .object(["path":.string(path.path),"bytes":.integer(data.count),"sha256":.string(sha(data))])
}
private func save(_ value:JSONValue,_ path:URL)throws {try value.encoded().write(to:path,options:.withoutOverwriting)}
private func parsed(_ data:Data)throws->JSONValue {
    try ExactJSON.decode(data,limits:.init(bytes:64*1024*1024,depth:256,values:2_000_000,stringBytes:2*1024*1024))
}

private let contractKeys=["id","port","registry_name","command","payload_type","result_type",
                          "caller","owner_authority","submission","commit"]
private let fixedCases:[(String,String)] = {
    var rows:[(String,String)]=[
        ("P01-profile","program version"),
        ("P02-sigma-id","fixed Sigma implementations"),
        ("P03-sigma-abi","fixed Sigma implementations"),
        ("P04-operation-name","fixed Sigma implementations"),
        ("P05-operation-kind","fixed Sigma implementations"),
        ("P06-operation-implementation","fixed Sigma implementations")]
    for (i,key) in contractKeys.enumerated() {
        rows.append((String(format:"C%02d-contract-%@",i+1,key),"fixed Sigma implementations"))
    }
    rows += [
        ("S01-port","fixed host-response port"),
        ("S02-extra-field","object fields do not match schema"),
        ("S03-missing-owner","object fields do not match schema"),
        ("S04-missing-token","object fields do not match schema"),
        ("A01-result-Word","host response result Unit contract"),
        ("A02-token-Unit","host response token Word contract"),
        ("A03-owner-Word","host response Owner contract"),
        ("A04-owner-missing-owner","host response Owner identity"),
        ("A05-owner-missing-size","host response Owner identity"),
        ("A06-owner-nominal-index","host response Owner nominal sort/source"),
        ("A07-owner-null","host response Owner nominal form"),
        ("A08-owner-wrong-sort","host response Owner nominal sort/source"),
        ("A09-size-null","host response Size nominal form"),
        ("A10-size-wrong-sort","host response Size nominal sort/source"),
        ("A11-size-negative-literal","host response Size literal nonnegative"),
        ("A12-owner-binding-mismatch","host response Owner complete binding correspondence"),
        ("E01-effect-tag","host response effect set"),
        ("E02-effect-family","host response effect set"),
        ("E03-effect-extra-field","host response effect atoms"),
        ("E04-effect-arity","host response exact effect arity"),
        ("E05-response-atom-missing","host response and Raise effects"),
        ("E06-raise-atom-missing","host response and Raise effects"),
        ("E07-response-extra-field","host response effect fields"),
        ("E08-raise-extra-field","host response effect fields"),
        ("E09-atom-family","host response effect fields"),
        ("E10-effect-owner-nominal-index","host response effect Owner correspondence"),
        ("T01-missing-owner-annotation","missing AST annotation"),
        ("T02-missing-token-annotation","missing AST annotation"),
        ("T03-node-kind","AST/annotation kind"),
        ("T04-child-order","exact source occurrence order"),
        ("T05-site-hash","site source identity"),
        ("T06-duplicate-site","duplicate source site"),
        ("B01-owner-direct-usage","host response Owner lexical binding")]
    return rows
}()

private final class Probe {
    let output:URL
    var files:[String:JSONValue]=[:]
    var observations:[JSONValue]=[]
    var decodeCalls=0,configurationCalls=0
    var current="input-preflight"
    init(output:URL) {self.output=output}

    func observedRead(_ url:URL)throws->Data {
        let data=try Data(contentsOf:url)
        let identity=pin(url,data)
        if let previous=files[url.path] {try require(previous==identity,"input changed: "+url.path)}
        files[url.path]=identity;return data
    }
    func pinnedRead(_ identity:JSONValue)throws->Data {
        _=try identity.members(exactly:["path","bytes","sha256"])
        let path=try identity.field("path").text()
        try require(path.hasPrefix("/"),"absolute pinned path")
        let url=URL(fileURLWithPath:path);let data=try observedRead(url)
        try require(pin(url,data)==identity,"exact input pin: "+path);return data
    }
    func recheckFiles()throws {
        for path in files.keys.sorted() {
            let data=try Data(contentsOf:URL(fileURLWithPath:path))
            try require(pin(URL(fileURLWithPath:path),data)==files[path],"post input changed: "+path)
        }
    }
    func observe(_ name:String,source:Data,response:Data,exit:Int32,
                 configuration:Bool?,expectedMessage:String?)throws {
        current=name
        let directory=output.appendingPathComponent(name,isDirectory:true)
        try FileManager.default.createDirectory(at:directory,withIntermediateDirectories:false)
        try source.write(to:directory.appendingPathComponent("source.json"),options:.withoutOverwriting)
        try response.write(to:directory.appendingPathComponent("frontend.stdout"),options:.withoutOverwriting)
        var result="accepted",actual:JSONValue = .null,unexpected:JSONValue = .null
        decodeCalls+=1
        do {
            let program=try decodeCompilerExchange(source:source,response:response,actualExitCode:exit)
            if let configured=configuration {
                configurationCalls+=1
                try validateExternalDemandConfiguration(program,configured:configured)
            }
        } catch let error as ProtocolError {
            result="protocol-rejected";actual=error.diagnostic
        } catch {
            result="unexpected-error"
            unexpected = .object(["type":.string(String(reflecting:type(of:error))),
                                  "description":.string(String(describing:error))])
        }
        let expected=expectedMessage.map(expectedDiagnostic) ?? .null
        let matched=expectedMessage == nil ? result == "accepted" : result == "protocol-rejected" && actual == expected
        let row:JSONValue = .object(["case":.string(name),"decode_result":.string(result),
            "actual_diagnostic":actual,"unexpected_error":unexpected,"expected_diagnostic":expected,
            "expectation_matched":.bool(matched),"compiler_actual_exit_supplied":.integer(Int(exit)),
            "configuration":configuration.map(JSONValue.bool) ?? .null,
            "source":pin(directory.appendingPathComponent("source.json"),source),
            "response":pin(directory.appendingPathComponent("frontend.stdout"),response)])
        // Persist the actual first observation, including an unexpected result,
        // before the assertion or any later case can fail.
        try save(row,directory.appendingPathComponent("observation.json"))
        observations.append(row)
        try require(matched,"first differing decoder observation: "+name)
    }

    func run(manifestURL:URL)throws {
        let manifest=try parsed(observedRead(manifestURL))
        try require(try manifest.field("schema").text()=="src1-response-decoder-mutations-v1","mutation manifest schema")
        try require(try manifest.field("construction_complete").boolean() && !manifest.field("decoder_executed").boolean() && !manifest.field("candidate_executed").boolean(),"construction-only manifest")
        try require(try manifest.field("case_count").integerToken()=="49" && manifest.field("fixed_observation_count").integerToken()=="53","fixed finite counts")
        try require(try manifest.field("actual_compiler_exit_supplied").integerToken()=="0" && manifest.field("offer_only_actual_compiler_exit_supplied").integerToken()=="0","actual positive compiler exits")
        let basis=try manifest.field("decoder_source_basis")
        try require(try basis.field("revision").text()=="SRC1-R1" && basis.field("bytes").integerToken()=="51129" &&
                    basis.field("sha256").text()=="404f725c0a5b25d541ff07dc138cc1cd43120bd4bd9128eaeb5fb36228ca46b0","R1 decoder source basis")
        _=try pinnedRead(manifest.field("constructor"))
        let inputs=try manifest.field("inputs")
        let source=try pinnedRead(inputs.field("source")),response=try pinnedRead(inputs.field("response"))
        let onlySource=try pinnedRead(inputs.field("offer_only_source")),onlyResponse=try pinnedRead(inputs.field("offer_only_response"))
        let copies=try manifest.field("original_copies"),onlyCopies=try manifest.field("offer_only_copies")
        try require(try pinnedRead(copies.field("source"))==source && pinnedRead(copies.field("response"))==response,"complete original bytes preserved")
        try require(try pinnedRead(onlyCopies.field("source"))==onlySource && pinnedRead(onlyCopies.field("response"))==onlyResponse,"offer-only original bytes preserved")
        let rows=try manifest.field("cases").elements()
        try require(rows.count==fixedCases.count && fixedCases.count==49,"fixed decoder population")
        for (i,row) in rows.enumerated() {
            try require(try row.field("case").text()==fixedCases[i].0,"fixed case identity/order")
            try require(try row.field("expected_diagnostic")==expectedDiagnostic(fixedCases[i].1),"manifest cannot change expected diagnostic")
        }

        try observe("BASE-configured-positive",source:source,response:response,exit:0,configuration:true,expectedMessage:nil)
        try observe("OFFER-ONLY-decode-positive",source:onlySource,response:onlyResponse,exit:0,configuration:nil,expectedMessage:nil)
        try observe("BASE-unconfigured-negative",source:source,response:response,exit:0,configuration:false,
                    expectedMessage:"external request configuration/checked source receive count")
        try observe("OFFER-ONLY-unconfigured-negative",source:onlySource,response:onlyResponse,exit:0,configuration:false,
                    expectedMessage:"source response offer requires external request configuration")
        for (index,row) in rows.enumerated() {
            let (name,message)=fixedCases[index];current=name
            let metadata=try parsed(pinnedRead(row.field("mutation")))
            try require(try metadata.field("case").text()==name && metadata.field("expected_diagnostic")==expectedDiagnostic(message) && !metadata.field("actual_decoder_executed").boolean(),"fixed mutation metadata identity")
            try require(try metadata.field("source")==row.field("source") && metadata.field("response")==row.field("response"),"mutation/manifest exact input pin correspondence")
            try require(try metadata.field("actual_compiler_exit_supplied").integerToken()=="0","mutation actual compiler exit")
            let changedSource=try pinnedRead(row.field("source")),changedResponse=try pinnedRead(row.field("response"))
            try require(changedResponse != response,"negative exchange differs from actual original")
            if !name.hasPrefix("S") {try require(changedSource==source,"annotation/registry mutation preserves source bytes")}
            try observe(name,source:changedSource,response:changedResponse,exit:0,configuration:nil,expectedMessage:message)
        }
        try require(observations.count==53 && decodeCalls==53 && configurationCalls==3,"all fixed actual calls completed")
        try recheckFiles()
        let report:JSONValue = .object(["schema":.string("src1-decoder-actual-controls-v1"),"passed":.bool(true),
            "observations":.array(observations),"observation_count":.integer(observations.count),
            "decode_calls":.integer(decodeCalls),"configuration_calls":.integer(configurationCalls),
            "files":.array(files.keys.sorted().map{files[$0]!}),"input_pins_unchanged":.bool(true),
            "source_runtime_executed":.bool(false),"compiler_started":.bool(false),"worker_started":.bool(false),
            "scope":.string("53 finite synchronous production decoder/configuration observations; no runtime response, request/receipt, lifecycle, GPU, liveness or whole-PL proof.")])
        try save(report,output.appendingPathComponent("result.json"))
        print("{\"passed\":true,\"observations\":53,\"decoder_negative_cases\":49}")
    }

    func fail(_ error:any Error) {
        let report:JSONValue = .object(["schema":.string("src1-decoder-actual-controls-v1"),"passed":.bool(false),
            "first_failure_case":.string(current),"error_type":.string(String(reflecting:type(of:error))),
            "error":.string(String(describing:error)),"observations":.array(observations),
            "decode_calls":.integer(decodeCalls),"configuration_calls":.integer(configurationCalls),
            "files":.array(files.keys.sorted().map{files[$0]!}),"source_runtime_executed":.bool(false)])
        do {try save(report,output.appendingPathComponent("first-failure.json"))}
        catch {FileHandle.standardError.write(Data(("first-failure record save error: "+String(describing:error)+"\n").utf8))}
    }
}

@main
private struct Main {
    static func main() {
        var probe:Probe?
        do {
            let args=Array(CommandLine.arguments.dropFirst())
            try require(args.count==4 && args[0]=="--manifest" && args[2]=="--output-dir","usage: src1-decoder-cpu --manifest ABS --output-dir NEW_ABS")
            try require(args[1].hasPrefix("/") && args[3].hasPrefix("/"),"absolute paths required")
            let manifest=URL(fileURLWithPath:args[1]),output=URL(fileURLWithPath:args[3],isDirectory:true)
            try require(!FileManager.default.fileExists(atPath:output.path),"exclusive new output directory")
            try FileManager.default.createDirectory(at:output,withIntermediateDirectories:true)
            let current=Probe(output:output);probe=current
            try current.run(manifestURL:manifest)
        } catch {
            probe?.fail(error)
            FileHandle.standardError.write(Data(("SRC1 decoder first failure: "+String(describing:error)+"\n").utf8))
            Darwin.exit(1)
        }
    }
}
