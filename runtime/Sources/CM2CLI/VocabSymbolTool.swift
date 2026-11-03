import Foundation
import Dispatch
import RuntimeCore

/// Runs synchronously on the original parent task. Core owns the actual file
/// handle, authority check, close result, clock and sealed ToolInfo/receipt.
/// This adapter is trusted only for parsing the bytes that Core actually read.
struct TokenizerVocabSymbolTool: VocabSymbolTool {
    func perform(_ invocation: ToolInvocation, root: ToolReadRoot) -> ToolCallOutcome {
        let bytes: Data
        do { bytes = try root.readAndClose() }
        catch { return root.finish(.readFailure) }
        do {
            let symbol = try VocabSymbolParser.lookup(bytes, token: invocation.token)
            return root.finish(.symbol(utf8: symbol))
        } catch let error as VocabSymbolParseFailure {
            return root.finish(.sourceFailure(error.code, detail: error.diagnostic))
        } catch {
            return root.finish(.infrastructure(detail: .object([
                "operation": .string("tokenizer-json-parse"),
                "cause": .string(String(describing: error))
            ])))
        }
    }
}

struct VocabSymbolParseFailure: Error, Sendable {
    let code: ToolFailureCode
    let message: String
    let offset: Int?
    init(_ code: ToolFailureCode, _ message: String, offset: Int? = nil) {
        self.code = code; self.message = message; self.offset = offset
    }
    init(_ message: String, offset: Int? = nil) {
        self.init(.schemaFailure, message, offset: offset)
    }
    var diagnostic: JSONValue {
        .object(["parser": .string("src1b-byte-json-v1"), "detail": .string(message),
                 "byte_offset": offset.map { .number(String($0)) } ?? .null])
    }
}

/// JSON keys and values use Data equality, never String canonical equivalence.
/// Strings below are *unescaped* Unicode scalar UTF-8. No ByteLevel decoding
/// or normalization occurs. The parser rejects duplicates before dictionaries
/// could silently replace an entry, including duplicates spelled with escapes.
enum VocabSymbolParser {
    private indirect enum Node {
        case object([Data: Node]), array([Node]), string(Data), number(Data)
        case boolean(Bool), null
    }

    static func lookup(_ data: Data, token: UInt64) throws -> Data {
        var parser = Parser(data)
        let root = try parser.document()
        guard case .object(let top) = root,
              case .object(let model)? = top[Data("model".utf8)],
              case .object(let vocab)? = model[Data("vocab".utf8)],
              case .array(let added)? = top[Data("added_tokens".utf8)] else {
            throw VocabSymbolParseFailure("expected /model/vocab object and /added_tokens array")
        }
        var byID: [UInt64: Data] = [:]
        byID.reserveCapacity(vocab.count)
        for (symbol, value) in vocab {
            let id = try identifier(value, path: "/model/vocab")
            guard id <= 151642 else { throw VocabSymbolParseFailure("ordinary vocabulary ID outside 0...151642") }
            guard byID.updateValue(symbol, forKey: id) == nil else {
                throw VocabSymbolParseFailure(.duplicateMapping, "duplicate ordinary vocabulary ID \(id)")
            }
        }
        var addedIDs = Set<UInt64>(), addedSymbols = Set<Data>()
        for entry in added {
            guard case .object(let object) = entry,
                  let rawID = object[Data("id".utf8)],
                  case .string(let content)? = object[Data("content".utf8)] else {
                throw VocabSymbolParseFailure("added token requires integer id and string content")
            }
            let id = try identifier(rawID, path: "/added_tokens/id")
            guard (151643...151668).contains(id) else {
                throw VocabSymbolParseFailure("added vocabulary ID outside 151643...151668")
            }
            guard addedIDs.insert(id).inserted && addedSymbols.insert(content).inserted && vocab[content] == nil else {
                throw VocabSymbolParseFailure(.duplicateMapping, "duplicate added token ID or symbol")
            }
        }
        guard token < 151936 else {
            throw VocabSymbolParseFailure(.argumentOutOfRange, "token \(token) outside model vocabulary")
        }
        if addedIDs.contains(token) {
            throw VocabSymbolParseFailure(.addedTokenUnsupported, "token \(token) is an added token, not an ordinary BPE symbol")
        }
        guard let symbol = byID[token] else {
            throw VocabSymbolParseFailure(.tokenMissing, "token \(token) absent from ordinary vocabulary")
        }
        return symbol
    }

    private static func identifier(_ node: Node, path: String) throws -> UInt64 {
        guard case .number(let raw) = node, !raw.isEmpty,
              raw.allSatisfy({ (48...57).contains($0) }) else {
            throw VocabSymbolParseFailure("\(path): ID must be a nonnegative JSON integer")
        }
        var result: UInt64 = 0
        for digit in raw {
            let (multiplied, overflow1) = result.multipliedReportingOverflow(by: 10)
            let (next, overflow2) = multiplied.addingReportingOverflow(UInt64(digit - 48))
            guard !overflow1 && !overflow2 else { throw VocabSymbolParseFailure("\(path): ID overflow") }
            result = next
        }
        return result
    }

    private struct Parser {
        let bytes: [UInt8]
        let started = DispatchTime.now().uptimeNanoseconds
        var offset = 0, nodes = 0, nextClockCheck = 0
        init(_ data: Data) { bytes = Array(data) }

        mutating func document() throws -> Node {
            guard bytes.count <= 12 * 1024 * 1024 else { throw failure("JSON exceeds 12 MiB") }
            let root = try value(depth: 0)
            whitespace()
            guard offset == bytes.count else { throw failure("trailing JSON bytes") }
            return root
        }
        func failure(_ message: String) -> VocabSymbolParseFailure {
            VocabSymbolParseFailure(message, offset: offset)
        }
        mutating func checkWork() throws {
            if offset >= nextClockCheck {
                nextClockCheck = offset + 16384
                if DispatchTime.now().uptimeNanoseconds - started > 2_000_000_000 {
                    throw VocabSymbolParseFailure(.budgetExceeded, "JSON parse observed more than 2 seconds", offset: offset)
                }
            }
        }
        mutating func whitespace() {
            while offset < bytes.count && [9, 10, 13, 32].contains(bytes[offset]) { offset += 1 }
        }
        mutating func consume(_ byte: UInt8) -> Bool {
            guard offset < bytes.count && bytes[offset] == byte else { return false }
            offset += 1; return true
        }
        mutating func value(depth: Int) throws -> Node {
            whitespace(); try checkWork()
            nodes += 1
            guard depth <= 128 && nodes <= 2_000_000 else { throw failure("JSON depth or node bound") }
            guard offset < bytes.count else { throw failure("missing JSON value") }
            switch bytes[offset] {
            case 123:
                offset += 1; whitespace()
                var object: [Data: Node] = [:]
                if consume(125) { return .object(object) }
                while true {
                    whitespace()
                    let key = try string()
                    guard object[key] == nil else {
                        throw VocabSymbolParseFailure(.duplicateMapping, "duplicate unescaped JSON byte key", offset: offset)
                    }
                    whitespace()
                    guard consume(58) else { throw failure("expected object colon") }
                    object[key] = try value(depth: depth + 1)
                    whitespace()
                    if consume(125) { return .object(object) }
                    guard consume(44) else { throw failure("expected object comma or end") }
                }
            case 91:
                offset += 1; whitespace()
                var array: [Node] = []
                if consume(93) { return .array(array) }
                while true {
                    array.append(try value(depth: depth + 1)); whitespace()
                    if consume(93) { return .array(array) }
                    guard consume(44) else { throw failure("expected array comma or end") }
                }
            case 34: return .string(try string())
            case 116: try literal([116,114,117,101]); return .boolean(true)
            case 102: try literal([102,97,108,115,101]); return .boolean(false)
            case 110: try literal([110,117,108,108]); return .null
            case 45, 48...57: return .number(try number())
            default: throw failure("invalid JSON value")
            }
        }
        mutating func literal(_ expected: [UInt8]) throws {
            guard offset + expected.count <= bytes.count &&
                    bytes[offset..<(offset + expected.count)].elementsEqual(expected) else {
                throw failure("invalid JSON literal")
            }
            offset += expected.count
        }
        mutating func number() throws -> Data {
            let start = offset
            _ = consume(45)
            guard offset < bytes.count else { throw failure("truncated number") }
            if consume(48) {
                if offset < bytes.count && (48...57).contains(bytes[offset]) { throw failure("number with leading zero") }
            } else {
                guard offset < bytes.count && (49...57).contains(bytes[offset]) else { throw failure("invalid integer part") }
                offset += 1
                while offset < bytes.count && (48...57).contains(bytes[offset]) { offset += 1 }
            }
            if consume(46) {
                let fraction = offset
                while offset < bytes.count && (48...57).contains(bytes[offset]) { offset += 1 }
                guard offset > fraction else { throw failure("empty fractional part") }
            }
            if consume(101) || consume(69) {
                if !consume(43) { _ = consume(45) }
                let exponent = offset
                while offset < bytes.count && (48...57).contains(bytes[offset]) { offset += 1 }
                guard offset > exponent else { throw failure("empty exponent") }
            }
            return Data(bytes[start..<offset])
        }
        mutating func string() throws -> Data {
            guard consume(34) else { throw failure("expected JSON string") }
            var output = [UInt8]()
            while offset < bytes.count {
                try checkWork()
                let byte = bytes[offset]; offset += 1
                if byte == 34 {
                    let result = Data(output)
                    // Validation only: the String is never used as a key and
                    // never re-encoded. Invalid/overlong UTF-8 is not repaired.
                    guard String(data: result, encoding: .utf8) != nil else { throw failure("invalid UTF-8 string") }
                    return result
                }
                guard byte >= 32 else { throw failure("unescaped JSON control byte") }
                if byte != 92 { output.append(byte); continue }
                guard offset < bytes.count else { throw failure("truncated escape") }
                let escaped = bytes[offset]; offset += 1
                switch escaped {
                case 34, 47, 92: output.append(escaped)
                case 98: output.append(8)
                case 102: output.append(12)
                case 110: output.append(10)
                case 114: output.append(13)
                case 116: output.append(9)
                case 117:
                    let high = try hex4()
                    let scalar: UInt32
                    if (0xD800...0xDBFF).contains(high) {
                        guard consume(92) && consume(117) else { throw failure("unpaired high surrogate") }
                        let low = try hex4()
                        guard (0xDC00...0xDFFF).contains(low) else { throw failure("invalid low surrogate") }
                        scalar = 0x10000 + ((high - 0xD800) << 10) + low - 0xDC00
                    } else {
                        guard !(0xDC00...0xDFFF).contains(high) else { throw failure("unpaired low surrogate") }
                        scalar = high
                    }
                    appendUTF8(scalar, to: &output)
                default: throw failure("unknown JSON escape")
                }
            }
            throw failure("unterminated JSON string")
        }
        mutating func hex4() throws -> UInt32 {
            var value: UInt32 = 0
            for _ in 0..<4 {
                guard offset < bytes.count else { throw failure("truncated unicode escape") }
                let byte = bytes[offset]; offset += 1
                let digit: UInt32
                switch byte {
                case 48...57: digit = UInt32(byte - 48)
                case 65...70: digit = UInt32(byte - 65 + 10)
                case 97...102: digit = UInt32(byte - 97 + 10)
                default: throw failure("invalid unicode escape")
                }
                value = (value << 4) | digit
            }
            return value
        }
        func appendUTF8(_ scalar: UInt32, to bytes: inout [UInt8]) {
            if scalar <= 0x7F { bytes.append(UInt8(scalar)) }
            else if scalar <= 0x7FF {
                bytes += [UInt8(0xC0 | (scalar >> 6)), UInt8(0x80 | (scalar & 0x3F))]
            } else if scalar <= 0xFFFF {
                bytes += [UInt8(0xE0 | (scalar >> 12)), UInt8(0x80 | ((scalar >> 6) & 0x3F)), UInt8(0x80 | (scalar & 0x3F))]
            } else {
                bytes += [UInt8(0xF0 | (scalar >> 18)), UInt8(0x80 | ((scalar >> 12) & 0x3F)),
                          UInt8(0x80 | ((scalar >> 6) & 0x3F)), UInt8(0x80 | (scalar & 0x3F))]
            }
        }
    }
}
