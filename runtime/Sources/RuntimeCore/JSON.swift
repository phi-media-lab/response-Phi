import Foundation

/// The internal wire uses ASCII object keys. Numbers remain exact JSON tokens.
/// Object-language identifiers are string values, not dictionary keys.
public indirect enum JSONValue: Sendable, Equatable {
    case null
    case bool(Bool)
    case number(String)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public static func integer(_ value: Int) -> JSONValue { .number(String(value)) }
    public static func unsigned(_ value: UInt64) -> JSONValue { .number(String(value)) }

    public func field(_ key: String) throws -> JSONValue {
        guard case .object(let fields) = self, let value = fields[key] else {
            throw ExactJSONError("missing object field: \(key)")
        }
        return value
    }

    public func members(exactly expected: Set<String>? = nil) throws -> [String: JSONValue] {
        guard case .object(let fields) = self else { throw ExactJSONError("object required") }
        if let expected, Set(fields.keys) != expected {
            throw ExactJSONError("object fields do not match schema")
        }
        return fields
    }

    public func elements() throws -> [JSONValue] {
        guard case .array(let elements) = self else { throw ExactJSONError("array required") }
        return elements
    }

    public func text() throws -> String {
        guard case .string(let text) = self else { throw ExactJSONError("string required") }
        return text
    }

    public func boolean() throws -> Bool {
        guard case .bool(let value) = self else { throw ExactJSONError("boolean required") }
        return value
    }

    public func integerToken() throws -> String {
        guard case .number(let token) = self,
              !token.contains("."), !token.contains("e"), !token.contains("E") else {
            throw ExactJSONError("integer JSON token required")
        }
        // Validate even values constructed inside the trusted implementation.
        guard case .number(let parsed) = try ExactJSON.decode(Data(token.utf8)),
              parsed.utf8.elementsEqual(token.utf8) else {
            throw ExactJSONError("integer JSON token required")
        }
        return token
    }

    /// A wire writer, not Python's source-canonical encoding.
    public func encoded() throws -> Data {
        var bytes = [UInt8]()
        try appendEncoded(to: &bytes, sourceCanonical: false)
        return Data(bytes)
    }

    /// Matches json.dumps(..., sort_keys=True, separators=(",", ":"),
    /// ensure_ascii=False) for this profile's ASCII keys and integer numbers.
    /// Source syntax containing a floating-point number is not in this domain.
    public func sourceCanonicalEncoded() throws -> Data {
        var bytes = [UInt8]()
        try appendEncoded(to: &bytes, sourceCanonical: true)
        return Data(bytes)
    }

    private func appendEncoded(to bytes: inout [UInt8], sourceCanonical: Bool) throws {
        switch self {
        case .null: bytes.append(contentsOf: "null".utf8)
        case .bool(let value): bytes.append(contentsOf: (value ? "true" : "false").utf8)
        case .number(let token):
            if sourceCanonical {
                let integer = try integerToken()
                bytes.append(contentsOf: (integer == "-0" ? "0" : integer).utf8)
                return
            }
            guard case .number(let parsed) = try ExactJSON.decode(Data(token.utf8)),
                  parsed.utf8.elementsEqual(token.utf8) else {
                throw ExactJSONError("invalid constructed JSON number")
            }
            bytes.append(contentsOf: token.utf8)
        case .string(let value): Self.appendString(value, to: &bytes, sourceCanonical: sourceCanonical)
        case .array(let values):
            bytes.append(91)
            for (index, value) in values.enumerated() {
                if index != 0 { bytes.append(44) }
                try value.appendEncoded(to: &bytes, sourceCanonical: sourceCanonical)
            }
            bytes.append(93)
        case .object(let fields):
            bytes.append(123)
            for (index, key) in fields.keys.sorted().enumerated() {
                guard key.utf8.allSatisfy({ $0 < 128 }) else {
                    throw ExactJSONError("wire object keys must be ASCII")
                }
                if index != 0 { bytes.append(44) }
                Self.appendString(key, to: &bytes, sourceCanonical: sourceCanonical)
                bytes.append(58)
                try fields[key]!.appendEncoded(to: &bytes, sourceCanonical: sourceCanonical)
            }
            bytes.append(125)
        }
    }

    private static func appendString(_ value: String, to bytes: inout [UInt8], sourceCanonical: Bool) {
        let hex = Array("0123456789abcdef".utf8)
        bytes.append(34)
        for byte in value.utf8 {
            if sourceCanonical {
                let shortEscape: UInt8?
                switch byte {
                case 8: shortEscape = 98
                case 9: shortEscape = 116
                case 10: shortEscape = 110
                case 12: shortEscape = 102
                case 13: shortEscape = 114
                default: shortEscape = nil
                }
                if let shortEscape { bytes.append(contentsOf: [92, shortEscape]); continue }
            }
            switch byte {
            case 34, 92: bytes.append(92); bytes.append(byte)
            case 0..<32:
                bytes.append(contentsOf: [92, 117, 48, 48, hex[Int(byte >> 4)], hex[Int(byte & 15)]])
            default: bytes.append(byte)
            }
        }
        bytes.append(34)
    }

    public static func == (lhs: JSONValue, rhs: JSONValue) -> Bool {
        switch (lhs, rhs) {
        case (.null, .null): return true
        case (.bool(let a), .bool(let b)): return a == b
        case (.number(let a), .number(let b)): return a.utf8.elementsEqual(b.utf8)
        case (.string(let a), .string(let b)): return a.utf8.elementsEqual(b.utf8)
        case (.array(let a), .array(let b)): return a == b
        case (.object(let a), .object(let b)): return a == b // Keys are ASCII.
        default: return false
        }
    }
}

public struct ExactJSONError: Error, Sendable, CustomStringConvertible {
    public let description: String
    public init(_ description: String) { self.description = description }
}

public enum ExactJSON {
    public struct Limits: Sendable {
        public let bytes: Int
        public let depth: Int
        public let values: Int
        public let stringBytes: Int
        public init(bytes: Int = 32 * 1024 * 1024, depth: Int = 256,
                    values: Int = 500_000, stringBytes: Int = 4 * 1024 * 1024) {
            self.bytes = bytes; self.depth = depth
            self.values = values; self.stringBytes = stringBytes
        }
    }

    public static func decode(_ data: Data, limits: Limits = Limits()) throws -> JSONValue {
        guard limits.bytes > 0, (1...512).contains(limits.depth),
              limits.values > 0, limits.stringBytes > 0,
              data.count <= limits.bytes else { throw ExactJSONError("JSON resource limit") }
        var parser = Parser(bytes: Array(data), limits: limits)
        let value = try parser.value(depth: 0)
        parser.whitespace()
        guard parser.offset == parser.bytes.count else { throw parser.error("trailing bytes") }
        return value
    }

    private struct Parser {
        let bytes: [UInt8]
        let limits: Limits
        var offset = 0
        var valueCount = 0

        func error(_ message: String) -> ExactJSONError {
            ExactJSONError("\(message) at byte \(offset)")
        }

        mutating func whitespace() {
            while offset < bytes.count, [UInt8(32), 9, 10, 13].contains(bytes[offset]) { offset += 1 }
        }

        mutating func take(_ byte: UInt8) -> Bool {
            guard offset < bytes.count, bytes[offset] == byte else { return false }
            offset += 1; return true
        }

        mutating func require(_ byte: UInt8) throws {
            guard take(byte) else { throw error("unexpected byte or EOF") }
        }

        mutating func keyword(_ word: String, result: JSONValue) throws -> JSONValue {
            for byte in word.utf8 { try require(byte) }
            return result
        }

        mutating func value(depth: Int) throws -> JSONValue {
            whitespace()
            valueCount += 1
            guard depth <= limits.depth, valueCount <= limits.values else { throw error("JSON resource limit") }
            guard offset < bytes.count else { throw error("value required") }
            switch bytes[offset] {
            case 110: return try keyword("null", result: .null)
            case 116: return try keyword("true", result: .bool(true))
            case 102: return try keyword("false", result: .bool(false))
            case 34: return .string(try string())
            case 91:
                offset += 1; whitespace(); var values = [JSONValue]()
                if take(93) { return .array(values) }
                while true {
                    values.append(try value(depth: depth + 1)); whitespace()
                    if take(93) { return .array(values) }
                    try require(44)
                }
            case 123:
                offset += 1; whitespace(); var fields = [String: JSONValue]()
                if take(125) { return .object(fields) }
                while true {
                    whitespace(); let key = try string()
                    guard key.utf8.allSatisfy({ $0 < 128 }) else {
                        throw error("wire object keys must be ASCII")
                    }
                    guard fields[key] == nil else { throw error("duplicate object key") }
                    whitespace(); try require(58)
                    fields[key] = try value(depth: depth + 1); whitespace()
                    if take(125) { return .object(fields) }
                    try require(44)
                }
            case 45, 48...57: return .number(try number())
            default: throw error("unexpected value byte")
            }
        }

        mutating func number() throws -> String {
            let start = offset
            _ = take(45)
            if !take(48) {
                guard offset < bytes.count, (49...57).contains(bytes[offset]) else { throw error("number digit required") }
                offset += 1
                while offset < bytes.count, (48...57).contains(bytes[offset]) { offset += 1 }
            }
            if take(46) { try digits() }
            if take(101) || take(69) {
                if !take(43) { _ = take(45) }
                try digits()
            }
            return String(decoding: bytes[start..<offset], as: UTF8.self)
        }

        mutating func digits() throws {
            let start = offset
            while offset < bytes.count, (48...57).contains(bytes[offset]) { offset += 1 }
            guard offset > start else { throw error("number digit required") }
        }

        mutating func hex4() throws -> UInt32 {
            var result: UInt32 = 0
            for _ in 0..<4 {
                guard offset < bytes.count else { throw error("incomplete Unicode escape") }
                let byte = bytes[offset]; offset += 1
                let digit: UInt32
                switch byte {
                case 48...57: digit = UInt32(byte - 48)
                case 65...70: digit = UInt32(byte - 55)
                case 97...102: digit = UInt32(byte - 87)
                default: throw error("invalid Unicode escape")
                }
                result = result * 16 + digit
            }
            return result
        }

        mutating func string() throws -> String {
            try require(34)
            var decoded = [UInt8]()
            while offset < bytes.count {
                let byte = bytes[offset]; offset += 1
                if byte == 34 {
                    guard let result = String(bytes: decoded, encoding: .utf8) else { throw error("invalid UTF-8") }
                    return result
                }
                guard byte >= 32 else { throw error("unescaped control in string") }
                if byte != 92 { decoded.append(byte) }
                else {
                    guard offset < bytes.count else { throw error("incomplete escape") }
                    let escape = bytes[offset]; offset += 1
                    switch escape {
                    case 34, 47, 92: decoded.append(escape)
                    case 98: decoded.append(8)
                    case 102: decoded.append(12)
                    case 110: decoded.append(10)
                    case 114: decoded.append(13)
                    case 116: decoded.append(9)
                    case 117:
                        var scalar = try hex4()
                        if (0xD800...0xDBFF).contains(scalar) {
                            try require(92); try require(117)
                            let low = try hex4()
                            guard (0xDC00...0xDFFF).contains(low) else { throw error("invalid surrogate pair") }
                            scalar = 0x10000 + (scalar - 0xD800) * 1024 + low - 0xDC00
                        } else if (0xDC00...0xDFFF).contains(scalar) { throw error("unpaired low surrogate") }
                        guard let codepoint = UnicodeScalar(scalar) else { throw error("invalid Unicode scalar") }
                        decoded.append(contentsOf: String(codepoint).utf8)
                    default: throw error("invalid string escape")
                    }
                }
                guard decoded.count <= limits.stringBytes else { throw error("string resource limit") }
            }
            throw error("unterminated string")
        }
    }
}
