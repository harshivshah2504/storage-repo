package com.harshiv.githubdrive.core

/**
 * Emits JSON byte-compatible with CPython's `json.dumps`.
 *
 * The Flask app writes `_manifest.json` with `json.dumps(payload, indent=2)` and the release-body
 * marker with `json.dumps(meta, separators=(",", ":"), ensure_ascii=True)`. Both are re-read by the
 * web app, so the Android writer has to match Python's exact layout:
 *
 *  - indent mode: 2 spaces, `": "` between key and value, `,` then newline between items,
 *    and empty containers collapse to `{}` / `[]` on one line.
 *  - compact mode: `,` and `:` with no spaces at all.
 *  - ensure_ascii: every codepoint outside printable ASCII is escaped as `\uXXXX` (surrogate pairs
 *    emitted as two escapes, which is what Python does too).
 *
 * Values must be [Map] (insertion-ordered), [List], [String], [Boolean], [Int]/[Long], or null.
 */
object PyJson {

    fun compact(value: Any?): String = StringBuilder().also { write(it, value, null, 0) }.toString()

    fun indented(value: Any?, indent: Int = 2): String =
        StringBuilder().also { write(it, value, indent, 0) }.toString()

    private fun write(out: StringBuilder, value: Any?, indent: Int?, depth: Int) {
        when (value) {
            null -> out.append("null")
            is Boolean -> out.append(if (value) "true" else "false")
            is Int, is Long, is Short, is Byte -> out.append(value.toString())
            is Double -> out.append(formatDouble(value))
            is Float -> out.append(formatDouble(value.toDouble()))
            is String -> writeString(out, value)
            is Map<*, *> -> writeObject(out, value, indent, depth)
            is List<*> -> writeArray(out, value, indent, depth)
            is Array<*> -> writeArray(out, value.toList(), indent, depth)
            else -> writeString(out, value.toString())
        }
    }

    private fun writeObject(out: StringBuilder, map: Map<*, *>, indent: Int?, depth: Int) {
        if (map.isEmpty()) { out.append("{}"); return }
        out.append('{')
        val inner = depth + 1
        var first = true
        for ((k, v) in map) {
            if (!first) out.append(',')
            first = false
            newlineAndPad(out, indent, inner)
            writeString(out, k.toString())
            out.append(if (indent == null) ":" else ": ")
            write(out, v, indent, inner)
        }
        newlineAndPad(out, indent, depth)
        out.append('}')
    }

    private fun writeArray(out: StringBuilder, list: List<*>, indent: Int?, depth: Int) {
        if (list.isEmpty()) { out.append("[]"); return }
        out.append('[')
        val inner = depth + 1
        var first = true
        for (v in list) {
            if (!first) out.append(',')
            first = false
            newlineAndPad(out, indent, inner)
            write(out, v, indent, inner)
        }
        newlineAndPad(out, indent, depth)
        out.append(']')
    }

    private fun newlineAndPad(out: StringBuilder, indent: Int?, depth: Int) {
        if (indent == null) return
        out.append('\n')
        repeat(indent * depth) { out.append(' ') }
    }

    private fun formatDouble(value: Double): String {
        if (value == Math.floor(value) && !value.isInfinite() && Math.abs(value) < 1e16) {
            return "${value.toLong()}.0"
        }
        return value.toString()
    }

    private fun writeString(out: StringBuilder, value: String) {
        out.append('"')
        for (ch in value) {
            when {
                ch == '"' -> out.append("\\\"")
                ch == '\\' -> out.append("\\\\")
                ch.code == 0x0A -> out.append("\\n")
                ch.code == 0x0D -> out.append("\\r")
                ch.code == 0x09 -> out.append("\\t")
                ch.code == 0x08 -> out.append("\\b")
                ch.code == 0x0C -> out.append("\\f")
                ch.code < 0x20 || ch.code > 0x7E -> out.append(unicodeEscape(ch))
                else -> out.append(ch)
            }
        }
        out.append('"')
    }

    private fun unicodeEscape(ch: Char): String {
        val hex = Integer.toHexString(ch.code).lowercase()
        return "\\u" + "0".repeat(4 - hex.length) + hex
    }
}
