package com.relayterm.app;

import java.nio.ByteBuffer;
import java.nio.CharBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.charset.CharsetDecoder;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

/**
 * A compact ANSI/VT terminal model. It intentionally owns no Android View
 * state, which makes parser behaviour deterministic in JVM tests and lets a
 * reconnecting profile keep its own grid.
 */
public final class AnsiTerminalModel {
    public static final int DEFAULT_COLOR = -1;
    private static final int MAX_SCROLLBACK = 2000;
    private static final int MAX_ESCAPE = 256;

    public static final class Cell {
        public String text;
        public int foreground;
        public int background;
        public boolean bold;
        public boolean faint;
        public boolean italic;
        public boolean underline;
        public boolean inverse;
        public boolean crossedOut;
        public boolean wideContinuation;

        public Cell() {
            reset();
        }

        public Cell(Cell other) {
            text = other.text;
            foreground = other.foreground;
            background = other.background;
            bold = other.bold;
            faint = other.faint;
            italic = other.italic;
            underline = other.underline;
            inverse = other.inverse;
            crossedOut = other.crossedOut;
            wideContinuation = other.wideContinuation;
        }

        public void reset() {
            text = " ";
            foreground = DEFAULT_COLOR;
            background = DEFAULT_COLOR;
            bold = false;
            faint = false;
            italic = false;
            underline = false;
            inverse = false;
            crossedOut = false;
            wideContinuation = false;
        }

        public Cell copy() {
            return new Cell(this);
        }
    }

    private enum ParseState { NORMAL, ESC, CSI, OSC, OSC_ESC }

    private int columns;
    private int rows;
    private ArrayList<Cell[]> screen;
    private final Deque<Cell[]> scrollback = new ArrayDeque<>();
    private ArrayList<Cell[]> savedMainScreen;
    private int cursorColumn;
    private int cursorRow;
    private int savedColumn;
    private int savedRow;
    private int savedForeground = DEFAULT_COLOR;
    private int savedBackground = DEFAULT_COLOR;
    private boolean cursorVisible = true;
    private boolean alternate;
    private int scrollTop;
    private int scrollBottom;

    private int foreground = DEFAULT_COLOR;
    private int background = DEFAULT_COLOR;
    private boolean bold;
    private boolean faint;
    private boolean italic;
    private boolean underline;
    private boolean inverse;
    private boolean crossedOut;

    private ParseState parseState = ParseState.NORMAL;
    private final StringBuilder escape = new StringBuilder();
    private final StringBuilder osc = new StringBuilder();
    private final CharsetDecoder decoder = StandardCharsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPLACE)
            .onUnmappableCharacter(CodingErrorAction.REPLACE);
    private byte[] incompleteUtf8 = new byte[0];
    private char pendingHighSurrogate;

    public AnsiTerminalModel() {
        this(100, 32);
    }

    public AnsiTerminalModel(int columns, int rows) {
        this.columns = clampColumns(columns);
        this.rows = clampRows(rows);
        initScreen();
    }

    public synchronized int columns() {
        return columns;
    }

    public synchronized int getColumns() {
        return columns;
    }

    public synchronized int rows() {
        return rows;
    }

    public synchronized int getRows() {
        return rows;
    }

    public synchronized int cursorColumn() {
        return cursorColumn;
    }

    public synchronized int cursorRow() {
        return cursorRow;
    }

    public synchronized boolean isCursorVisible() {
        return cursorVisible;
    }

    public synchronized boolean isAlternateScreen() {
        return alternate;
    }

    public synchronized void reset() {
        columns = clampColumns(columns);
        rows = clampRows(rows);
        scrollback.clear();
        savedMainScreen = null;
        alternate = false;
        foreground = DEFAULT_COLOR;
        background = DEFAULT_COLOR;
        bold = faint = italic = underline = inverse = crossedOut = false;
        cursorVisible = true;
        parseState = ParseState.NORMAL;
        escape.setLength(0);
        osc.setLength(0);
        incompleteUtf8 = new byte[0];
        pendingHighSurrogate = 0;
        decoder.reset();
        initScreen();
    }

    private static int clampColumns(int value) {
        return Math.max(2, Math.min(value <= 0 ? 100 : value, 400));
    }

    private static int clampRows(int value) {
        return Math.max(2, Math.min(value <= 0 ? 32 : value, 200));
    }

    private void initScreen() {
        screen = new ArrayList<>();
        for (int row = 0; row < rows; row++) screen.add(blankLine());
        cursorColumn = cursorRow = 0;
        scrollTop = 0;
        scrollBottom = rows - 1;
    }

    private Cell[] blankLine() {
        Cell[] line = new Cell[columns];
        for (int i = 0; i < columns; i++) line[i] = new Cell();
        return line;
    }

    private static Cell[] copyLine(Cell[] source) {
        Cell[] result = new Cell[source.length];
        for (int i = 0; i < source.length; i++) result[i] = source[i].copy();
        return result;
    }

    private ArrayList<Cell[]> copyScreen(ArrayList<Cell[]> source) {
        ArrayList<Cell[]> result = new ArrayList<>();
        for (Cell[] line : source) result.add(copyLine(line));
        return result;
    }

    /** Feed an arbitrary byte fragment from a PTY output frame. */
    public synchronized void feed(byte[] bytes) {
        if (bytes == null || bytes.length == 0) return;
        byte[] combined = new byte[incompleteUtf8.length + bytes.length];
        System.arraycopy(incompleteUtf8, 0, combined, 0, incompleteUtf8.length);
        System.arraycopy(bytes, 0, combined, incompleteUtf8.length, bytes.length);
        ByteBuffer input = ByteBuffer.wrap(combined);
        CharBuffer output = CharBuffer.allocate(Math.max(256, combined.length * 2));
        try {
            while (true) {
                output.clear();
                java.nio.charset.CoderResult result = decoder.decode(input, output, false);
                output.flip();
                processCharacters(output);
                if (result.isUnderflow()) break;
                if (result.isOverflow()) continue;
                // Malformed input is configured to replace, but make progress
                // defensively if a platform decoder reports another result.
                if (input.hasRemaining()) input.position(input.position() + 1);
            }
        } catch (Exception ignored) {
            // Keep the grid responsive even for malformed terminal bytes.
        }
        incompleteUtf8 = new byte[input.remaining()];
        input.get(incompleteUtf8);
    }

    public synchronized void feed(String text) {
        if (text != null && !text.isEmpty()) feed(text.getBytes(StandardCharsets.UTF_8));
    }

    public void append(byte[] bytes) {
        feed(bytes);
    }

    public void append(String text) {
        feed(text);
    }

    private void processCharacters(CharBuffer chars) {
        while (chars.hasRemaining()) {
            char value = chars.get();
            if (pendingHighSurrogate != 0) {
                if (Character.isLowSurrogate(value)) {
                    processCodePoint(Character.toCodePoint(pendingHighSurrogate, value));
                    pendingHighSurrogate = 0;
                    continue;
                }
                processCodePoint(pendingHighSurrogate);
                pendingHighSurrogate = 0;
            }
            if (Character.isHighSurrogate(value)) {
                pendingHighSurrogate = value;
            } else {
                processCodePoint(value);
            }
        }
    }

    private void processCodePoint(int codePoint) {
        if (parseState == ParseState.NORMAL) {
            if (codePoint == 0x1B) {
                parseState = ParseState.ESC;
                escape.setLength(0);
                return;
            }
            if (codePoint == '\r') {
                cursorColumn = 0;
                return;
            }
            if (codePoint == '\n' || codePoint == 0x0B || codePoint == 0x0C) {
                lineFeed();
                return;
            }
            if (codePoint == '\b') {
                cursorColumn = Math.max(0, cursorColumn - 1);
                return;
            }
            if (codePoint == '\t') {
                int next = Math.min(columns - 1, ((cursorColumn / 8) + 1) * 8);
                cursorColumn = next;
                return;
            }
            if (codePoint < 0x20 || codePoint == 0x7F) return;
            putCodePoint(codePoint);
            return;
        }
        if (parseState == ParseState.ESC) {
            handleEscape(codePoint);
            return;
        }
        if (parseState == ParseState.CSI) {
            if (codePoint >= 0x40 && codePoint <= 0x7E) {
                executeCsi((char) codePoint, escape.toString());
                parseState = ParseState.NORMAL;
                escape.setLength(0);
            } else if (escape.length() < MAX_ESCAPE) {
                escape.appendCodePoint(codePoint);
            } else {
                parseState = ParseState.NORMAL;
                escape.setLength(0);
            }
            return;
        }
        if (parseState == ParseState.OSC) {
            if (codePoint == 0x07) {
                parseState = ParseState.NORMAL;
                osc.setLength(0);
            } else if (codePoint == 0x1B) {
                parseState = ParseState.OSC_ESC;
            } else if (osc.length() < MAX_ESCAPE) {
                osc.appendCodePoint(codePoint);
            }
            return;
        }
        if (parseState == ParseState.OSC_ESC) {
            if (codePoint == '\\') {
                parseState = ParseState.NORMAL;
                osc.setLength(0);
            } else {
                parseState = ParseState.OSC;
            }
        }
    }

    private void handleEscape(int codePoint) {
        switch (codePoint) {
            case '[':
                parseState = ParseState.CSI;
                escape.setLength(0);
                break;
            case ']':
                parseState = ParseState.OSC;
                osc.setLength(0);
                break;
            case '7':
                saveCursor();
                parseState = ParseState.NORMAL;
                break;
            case '8':
                restoreCursor();
                parseState = ParseState.NORMAL;
                break;
            case 'c':
                reset();
                break;
            case 'D':
                lineFeed();
                parseState = ParseState.NORMAL;
                break;
            case 'M':
                reverseIndex();
                parseState = ParseState.NORMAL;
                break;
            case 'E':
                cursorColumn = 0;
                lineFeed();
                parseState = ParseState.NORMAL;
                break;
            case 'H':
                parseState = ParseState.NORMAL;
                break;
            case '=':
            case '>':
            case '(':
            case ')':
                // Keypad/charset selection does not alter the visible grid.
                parseState = ParseState.NORMAL;
                break;
            default:
                parseState = ParseState.NORMAL;
                break;
        }
    }

    private void putCodePoint(int codePoint) {
        int width = codePointWidth(codePoint);
        if (width <= 0) {
            if (cursorColumn > 0) {
                Cell previous = screen.get(cursorRow)[Math.min(columns - 1, cursorColumn - 1)];
                if (!previous.wideContinuation) previous.text += new String(Character.toChars(codePoint));
            }
            return;
        }
        if (cursorColumn >= columns || (width == 2 && cursorColumn == columns - 1)) {
            cursorColumn = 0;
            lineFeed();
        }
        Cell[] line = screen.get(cursorRow);
        Cell cell = line[cursorColumn];
        cell.text = new String(Character.toChars(codePoint));
        cell.foreground = foreground;
        cell.background = background;
        cell.bold = bold;
        cell.faint = faint;
        cell.italic = italic;
        cell.underline = underline;
        cell.inverse = inverse;
        cell.crossedOut = crossedOut;
        cell.wideContinuation = false;
        if (width == 2 && cursorColumn + 1 < columns) {
            Cell continuation = line[cursorColumn + 1];
            continuation.reset();
            continuation.foreground = foreground;
            continuation.background = background;
            continuation.wideContinuation = true;
        }
        cursorColumn += width;
        if (cursorColumn > columns) cursorColumn = columns;
    }

    private static int codePointWidth(int value) {
        int type = Character.getType(value);
        if (type == Character.NON_SPACING_MARK || type == Character.COMBINING_SPACING_MARK
                || type == Character.ENCLOSING_MARK || value == 0x200D) return 0;
        if (value >= 0x1100 && (value <= 0x115F || value == 0x2329 || value == 0x232A
                || (value >= 0x2E80 && value <= 0xA4CF)
                || (value >= 0xAC00 && value <= 0xD7A3)
                || (value >= 0xF900 && value <= 0xFAFF)
                || (value >= 0xFE10 && value <= 0xFE19)
                || (value >= 0xFE30 && value <= 0xFE6F)
                || (value >= 0xFF00 && value <= 0xFF60)
                || (value >= 0xFFE0 && value <= 0xFFE6)
                || (value >= 0x1F300 && value <= 0x1FAFF))) return 2;
        return 1;
    }

    private void lineFeed() {
        if (cursorRow == scrollBottom) {
            scrollUp(1);
        } else {
            cursorRow = Math.min(rows - 1, cursorRow + 1);
        }
    }

    private void scrollUp(int count) {
        for (int n = 0; n < count; n++) {
            Cell[] removed = screen.remove(scrollTop);
            if (!alternate) {
                scrollback.addLast(copyLine(removed));
                while (scrollback.size() > MAX_SCROLLBACK) scrollback.removeFirst();
            }
            screen.add(scrollBottom, blankLine());
        }
    }

    private void reverseIndex() {
        if (cursorRow > scrollTop) {
            cursorRow--;
            return;
        }
        screen.remove(scrollBottom);
        screen.add(scrollTop, blankLine());
    }

    private void executeCsi(char command, String raw) {
        boolean privateMode = raw.startsWith("?");
        String value = raw;
        while (!value.isEmpty() && (value.charAt(0) == '?' || value.charAt(0) == '>'
                || value.charAt(0) == '!')) value = value.substring(1);
        String[] pieces = value.split("[;:]", -1);
        int[] params = new int[Math.max(1, pieces.length)];
        for (int i = 0; i < params.length; i++) {
            if (i >= pieces.length || pieces[i].isEmpty()) params[i] = 0;
            else {
                try { params[i] = Integer.parseInt(pieces[i]); }
                catch (NumberFormatException ignored) { params[i] = 0; }
            }
        }
        switch (command) {
            case 'A': cursorRow = Math.max(scrollTop, cursorRow - positive(params[0])); break;
            case 'B': cursorRow = Math.min(scrollBottom, cursorRow + positive(params[0])); break;
            case 'C':
            case 'a': cursorColumn = Math.min(columns, cursorColumn + positive(params[0])); break;
            case 'D': cursorColumn = Math.max(0, cursorColumn - positive(params[0])); break;
            case 'E': cursorRow = Math.min(scrollBottom, cursorRow + positive(params[0])); cursorColumn = 0; break;
            case 'F': cursorRow = Math.max(scrollTop, cursorRow - positive(params[0])); cursorColumn = 0; break;
            case 'G':
            case '`': cursorColumn = clamp(positive(params[0]) - 1, 0, columns - 1); break;
            case 'd': cursorRow = clamp(positive(params[0]) - 1, scrollTop, scrollBottom); break;
            case 'H':
            case 'f':
                cursorRow = clamp(positive(params[0]) - 1, scrollTop, scrollBottom);
                cursorColumn = clamp(positive(params.length > 1 ? params[1] : 0) - 1, 0, columns - 1);
                break;
            case 'J': eraseDisplay(params[0]); break;
            case 'K': eraseLine(params[0]); break;
            case 'm': applySgr(params); break;
            case 'r':
                int top = positive(params[0]) - 1;
                int bottom = positive(params.length > 1 ? params[1] : 0) - 1;
                scrollTop = clamp(top, 0, rows - 1);
                scrollBottom = clamp(bottom <= 0 ? rows - 1 : bottom, scrollTop, rows - 1);
                cursorColumn = 0;
                cursorRow = scrollTop;
                break;
            case 's': saveCursor(); break;
            case 'u': restoreCursor(); break;
            case '@': insertChars(positive(params[0])); break;
            case 'P': deleteChars(positive(params[0])); break;
            case 'X': eraseChars(positive(params[0])); break;
            case 'L': insertLines(positive(params[0])); break;
            case 'M': deleteLines(positive(params[0])); break;
            case 'S': scrollUp(positive(params[0])); break;
            case 'T': scrollDown(positive(params[0])); break;
            case 'h':
            case 'l':
                if (privateMode) applyPrivateMode(command == 'h', params);
                break;
            case 'q':
                // Cursor style selection; rendering always uses a block cursor.
                break;
            default: break;
        }
    }

    private static int positive(int value) { return value <= 0 ? 1 : value; }
    private static int clamp(int value, int low, int high) { return Math.max(low, Math.min(high, value)); }

    private void eraseDisplay(int mode) {
        if (mode == 2 || mode == 3) {
            for (int row = 0; row < rows; row++) screen.set(row, blankLine());
            if (mode == 3) scrollback.clear();
            cursorColumn = cursorRow = 0;
            return;
        }
        if (mode == 1) {
            for (int row = 0; row <= cursorRow; row++) {
                int end = row == cursorRow ? cursorColumn : columns - 1;
                for (int col = 0; col <= end; col++) screen.get(row)[col].reset();
            }
        } else {
            for (int row = cursorRow; row < rows; row++) {
                int start = row == cursorRow ? cursorColumn : 0;
                for (int col = start; col < columns; col++) screen.get(row)[col].reset();
            }
        }
    }

    private void eraseLine(int mode) {
        Cell[] line = screen.get(cursorRow);
        int start = mode == 1 ? 0 : (mode == 2 ? 0 : cursorColumn);
        int end = mode == 0 ? columns - 1 : (mode == 1 ? cursorColumn : columns - 1);
        for (int col = start; col <= end; col++) line[col].reset();
    }

    private void insertChars(int count) {
        Cell[] line = screen.get(cursorRow);
        count = Math.min(count, columns - cursorColumn);
        for (int col = columns - 1; col >= cursorColumn + count; col--) line[col] = line[col - count];
        for (int col = cursorColumn; col < cursorColumn + count; col++) line[col] = new Cell();
    }

    private void deleteChars(int count) {
        Cell[] line = screen.get(cursorRow);
        count = Math.min(count, columns - cursorColumn);
        for (int col = cursorColumn; col < columns - count; col++) line[col] = line[col + count];
        for (int col = columns - count; col < columns; col++) line[col] = new Cell();
    }

    private void eraseChars(int count) {
        Cell[] line = screen.get(cursorRow);
        int end = Math.min(columns, cursorColumn + count);
        for (int col = cursorColumn; col < end; col++) line[col].reset();
    }

    private void insertLines(int count) {
        if (cursorRow < scrollTop || cursorRow > scrollBottom) return;
        count = Math.min(count, scrollBottom - cursorRow + 1);
        for (int n = 0; n < count; n++) {
            screen.remove(scrollBottom);
            screen.add(cursorRow, blankLine());
        }
    }

    private void deleteLines(int count) {
        if (cursorRow < scrollTop || cursorRow > scrollBottom) return;
        count = Math.min(count, scrollBottom - cursorRow + 1);
        for (int n = 0; n < count; n++) {
            screen.remove(cursorRow);
            screen.add(scrollBottom, blankLine());
        }
    }

    private void scrollDown(int count) {
        count = Math.min(count, scrollBottom - scrollTop + 1);
        for (int n = 0; n < count; n++) {
            screen.remove(scrollBottom);
            screen.add(scrollTop, blankLine());
        }
    }

    private void applyPrivateMode(boolean enable, int[] params) {
        for (int param : params) {
            if (param == 25) cursorVisible = enable;
            else if (param == 1049 || param == 47 || param == 1047) {
                if (enable && !alternate) switchAlternate(true);
                else if (!enable && alternate) switchAlternate(false);
            }
        }
    }

    private void switchAlternate(boolean enable) {
        if (enable) {
            savedMainScreen = copyScreen(screen);
            saveCursor();
            alternate = true;
            initScreen();
        } else {
            if (savedMainScreen != null) screen = savedMainScreen;
            alternate = false;
            cursorColumn = clamp(savedColumn, 0, columns - 1);
            cursorRow = clamp(savedRow, 0, rows - 1);
            scrollTop = 0;
            scrollBottom = rows - 1;
            savedMainScreen = null;
        }
    }

    private void saveCursor() {
        savedColumn = cursorColumn;
        savedRow = cursorRow;
        savedForeground = foreground;
        savedBackground = background;
    }

    private void restoreCursor() {
        cursorColumn = clamp(savedColumn, 0, columns - 1);
        cursorRow = clamp(savedRow, scrollTop, scrollBottom);
        foreground = savedForeground;
        background = savedBackground;
    }

    private void applySgr(int[] params) {
        if (params.length == 0) params = new int[]{0};
        for (int i = 0; i < params.length; i++) {
            int value = params[i];
            if (value == 0) {
                foreground = background = DEFAULT_COLOR;
                bold = faint = italic = underline = inverse = crossedOut = false;
            } else if (value == 1) bold = true;
            else if (value == 2) faint = true;
            else if (value == 3) italic = true;
            else if (value == 4) underline = true;
            else if (value == 7) inverse = true;
            else if (value == 9) crossedOut = true;
            else if (value == 22) bold = faint = false;
            else if (value == 23) italic = false;
            else if (value == 24) underline = false;
            else if (value == 27) inverse = false;
            else if (value == 29) crossedOut = false;
            else if (value >= 30 && value <= 37) foreground = value - 30;
            else if (value == 39) foreground = DEFAULT_COLOR;
            else if (value >= 40 && value <= 47) background = value - 40;
            else if (value == 49) background = DEFAULT_COLOR;
            else if (value >= 90 && value <= 97) foreground = 8 + value - 90;
            else if (value >= 100 && value <= 107) background = 8 + value - 100;
            else if (value == 38 || value == 48) {
                boolean fg = value == 38;
                if (i + 1 < params.length && params[i + 1] == 5 && i + 2 < params.length) {
                    if (fg) foreground = clamp(params[i + 2], 0, 255);
                    else background = clamp(params[i + 2], 0, 255);
                    i += 2;
                } else if (i + 4 < params.length && params[i + 1] == 2) {
                    int rgb = (clamp(params[i + 2], 0, 255) << 16)
                            | (clamp(params[i + 3], 0, 255) << 8)
                            | clamp(params[i + 4], 0, 255);
                    if (fg) foreground = 0x1000000 | rgb;
                    else background = 0x1000000 | rgb;
                    i += 4;
                }
            }
        }
    }

    /** Deep copy of the visible grid for Canvas rendering. */
    public synchronized Cell[][] snapshot() {
        Cell[][] result = new Cell[rows][columns];
        for (int row = 0; row < rows; row++) {
            for (int col = 0; col < columns; col++) result[row][col] = screen.get(row)[col].copy();
        }
        return result;
    }

    public synchronized List<Cell[]> scrollbackSnapshot() {
        List<Cell[]> result = new ArrayList<>();
        for (Cell[] line : scrollback) result.add(copyLine(line));
        return result;
    }

    public synchronized Cell cellAt(int column, int row) {
        if (row < 0 || row >= rows || column < 0 || column >= columns) return new Cell();
        return screen.get(row)[column].copy();
    }

    public synchronized Cell getCell(int column, int row) {
        return cellAt(column, row);
    }

    public synchronized String text() {
        StringBuilder result = new StringBuilder();
        for (int row = 0; row < rows; row++) {
            if (row > 0) result.append('\n');
            for (Cell cell : screen.get(row)) if (!cell.wideContinuation) result.append(cell.text);
        }
        return result.toString();
    }

    public synchronized void resize(int newColumns, int newRows) {
        newColumns = clampColumns(newColumns);
        newRows = clampRows(newRows);
        if (newColumns == columns && newRows == rows) return;
        ArrayList<Cell[]> old = screen;
        ArrayList<Cell[]> oldSaved = savedMainScreen;
        int oldColumns = columns;
        int oldRows = rows;
        columns = newColumns;
        rows = newRows;
        screen = new ArrayList<>();
        for (int row = 0; row < newRows; row++) {
            Cell[] line = blankLine();
            int sourceRow = row + Math.max(0, oldRows - newRows);
            if (sourceRow >= 0 && sourceRow < oldRows) {
                Cell[] source = old.get(sourceRow);
                for (int col = 0; col < Math.min(oldColumns, newColumns); col++) line[col] = source[col].copy();
            }
            screen.add(line);
        }
        if (oldSaved != null) {
            savedMainScreen = resizeCopy(oldSaved, oldColumns, oldRows, newColumns, newRows);
        }
        cursorColumn = clamp(cursorColumn, 0, columns - 1);
        cursorRow = clamp(cursorRow, 0, rows - 1);
        scrollTop = 0;
        scrollBottom = rows - 1;
    }

    private ArrayList<Cell[]> resizeCopy(
            ArrayList<Cell[]> source, int sourceColumns, int sourceRows,
            int targetColumns, int targetRows) {
        ArrayList<Cell[]> result = new ArrayList<>();
        for (int row = 0; row < targetRows; row++) {
            Cell[] line = new Cell[targetColumns];
            for (int col = 0; col < targetColumns; col++) line[col] = new Cell();
            int sourceRow = row + Math.max(0, sourceRows - targetRows);
            if (sourceRow >= 0 && sourceRow < source.size()) {
                Cell[] oldLine = source.get(sourceRow);
                for (int col = 0; col < Math.min(targetColumns, sourceColumns); col++) {
                    line[col] = oldLine[col].copy();
                }
            }
            result.add(line);
        }
        return result;
    }

    public static int paletteColor(int index) {
        int[] base = {
                0x000000, 0xAA0000, 0x00AA00, 0xAA5500,
                0x0000AA, 0xAA00AA, 0x00AAAA, 0xAAAAAA,
                0x555555, 0xFF5555, 0x55FF55, 0xFFFF55,
                0x5555FF, 0xFF55FF, 0x55FFFF, 0xFFFFFF
        };
        if (index < 0) return 0;
        if (index < 16) return base[index];
        int value = index - 16;
        if (value < 216) {
            int r = value / 36;
            int g = (value / 6) % 6;
            int b = value % 6;
            return ((r == 0 ? 0 : 55 + r * 40) << 16)
                    | ((g == 0 ? 0 : 55 + g * 40) << 8)
                    | (b == 0 ? 0 : 55 + b * 40);
        }
        int gray = 8 + (value - 216) * 10;
        gray = clamp(gray, 0, 255);
        return (gray << 16) | (gray << 8) | gray;
    }
}
