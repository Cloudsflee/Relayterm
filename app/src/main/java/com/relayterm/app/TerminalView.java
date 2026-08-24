package com.relayterm.app;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.util.AttributeSet;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.inputmethod.BaseInputConnection;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;
import android.view.inputmethod.InputMethodManager;

import java.util.List;

/** Canvas renderer for {@link AnsiTerminalModel}; no WebView or nested scrolling. */
public final class TerminalView extends View {
    public interface Listener {
        void onResize(int columns, int rows);
        void onInput(byte[] bytes);
    }

    private static final int DEFAULT_BACKGROUND = Color.rgb(13, 18, 24);
    private static final int DEFAULT_FOREGROUND = Color.rgb(226, 235, 237);
    private static final int CURSOR_COLOR = Color.rgb(87, 214, 180);

    private final Paint textPaint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.SUBPIXEL_TEXT_FLAG);
    private final Paint backgroundPaint = new Paint();
    private final Handler main = new Handler(Looper.getMainLooper());
    private AnsiTerminalModel model = new AnsiTerminalModel();
    private Listener listener;
    private float cellWidth;
    private float cellHeight;
    private float baselineOffset;
    private int lastColumns = 100;
    private int lastRows = 32;
    private int scrollOffset;
    private boolean resizeSuspended;
    private int pendingColumns = -1;
    private int pendingRows = -1;
    private final Runnable resizeCommit = this::commitPendingResize;
    private final TerminalInputCodec.Composer composer = new TerminalInputCodec.Composer();

    public TerminalView(Context context) {
        this(context, null);
    }

    public TerminalView(Context context, AttributeSet attrs) {
        super(context, attrs);
        textPaint.setTypeface(Typeface.create(Typeface.MONOSPACE, Typeface.NORMAL));
        textPaint.setTextSize(sp(13));
        setBackgroundColor(DEFAULT_BACKGROUND);
        setFocusable(true);
        setFocusableInTouchMode(true);
        setContentDescription("交互式终端");
        updateMetrics();
    }

    public void setListener(Listener listener) {
        this.listener = listener;
    }

    public void setModel(AnsiTerminalModel model) {
        this.model = model == null ? new AnsiTerminalModel(lastColumns, lastRows) : model;
        composer.clear();
        this.scrollOffset = 0;
        this.model.resize(lastColumns, lastRows);
        invalidate();
    }

    public AnsiTerminalModel getModel() {
        return model;
    }

    public int getTerminalColumns() {
        return lastColumns;
    }

    public int getTerminalRows() {
        return lastRows;
    }

    public void append(byte[] bytes) {
        model.feed(bytes);
        if (scrollOffset == 0) invalidate();
    }

    public void clearTerminal() {
        composer.clear();
        model.reset();
        model.resize(lastColumns, lastRows);
        scrollOffset = 0;
        invalidate();
    }

    public void pageUp() {
        int maximum = model.scrollbackSnapshot().size();
        scrollOffset = Math.min(maximum, scrollOffset + Math.max(1, lastRows - 1));
        invalidate();
    }

    public void pageDown() {
        scrollOffset = Math.max(0, scrollOffset - Math.max(1, lastRows - 1));
        invalidate();
    }

    public void scrollToBottom() {
        scrollOffset = 0;
        invalidate();
    }

    /**
     * Hold grid/PTTY resize notifications while the IME changes the window
     * height. The final dimensions are committed together when animation
     * ends, preventing a stream of intermediate resize events.
     */
    public void setResizeSuspended(boolean suspended) {
        resizeSuspended = suspended;
        if (suspended) {
            main.removeCallbacks(resizeCommit);
        } else {
            main.removeCallbacks(resizeCommit);
            commitPendingResize();
        }
    }

    public boolean isResizeSuspended() {
        return resizeSuspended;
    }

    /** Visible for diagnostics/tests; this text has not been sent to the PTY. */
    public String getComposingText() {
        return composer.value();
    }

    /** Apply a measured size immediately, useful after an inset animation. */
    public void commitResize() {
        main.removeCallbacks(resizeCommit);
        commitPendingResize();
    }

    private void scheduleResizeCommit() {
        main.removeCallbacks(resizeCommit);
        // A short debounce also covers pre-Android-30 devices where the
        // compat animation callback may not expose every IME transition.
        main.postDelayed(resizeCommit, 150L);
    }

    private void commitPendingResize() {
        if (resizeSuspended || pendingColumns < 2 || pendingRows < 2) return;
        int columns = pendingColumns;
        int rows = pendingRows;
        pendingColumns = pendingRows = -1;
        if (columns == lastColumns && rows == lastRows) return;
        lastColumns = columns;
        lastRows = rows;
        model.resize(columns, rows);
        if (listener != null) listener.onResize(columns, rows);
        invalidate();
    }

    private void updateMetrics() {
        cellWidth = Math.max(1f, (float) Math.ceil(textPaint.measureText("M")));
        Paint.FontMetrics metrics = textPaint.getFontMetrics();
        cellHeight = Math.max(1f, (float) Math.ceil(metrics.descent - metrics.ascent));
        baselineOffset = -metrics.ascent;
    }

    @Override
    protected void onSizeChanged(int width, int height, int oldWidth, int oldHeight) {
        super.onSizeChanged(width, height, oldWidth, oldHeight);
        int availableWidth = Math.max(1, width - getPaddingLeft() - getPaddingRight());
        int availableHeight = Math.max(1, height - getPaddingTop() - getPaddingBottom());
        int columns = Math.max(2, Math.min(400, (int) Math.floor(availableWidth / cellWidth)));
        int rows = Math.max(2, Math.min(200, (int) Math.floor(availableHeight / cellHeight)));
        if (columns != lastColumns || rows != lastRows
                || columns != pendingColumns || rows != pendingRows) {
            pendingColumns = columns;
            pendingRows = rows;
            if (!resizeSuspended) scheduleResizeCommit();
        }
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        AnsiTerminalModel.Cell[][] visible = model.snapshot();
        List<AnsiTerminalModel.Cell[]> history = model.scrollbackSnapshot();
        int modelRows = visible.length;
        int start = history.size() + modelRows - scrollOffset - lastRows;
        int cursorRow = model.cursorRow();
        int cursorColumn = model.cursorColumn();
        boolean drawCursor = scrollOffset == 0 && model.isCursorVisible() && hasWindowFocus();
        for (int viewRow = 0; viewRow < lastRows; viewRow++) {
            int logical = start + viewRow;
            AnsiTerminalModel.Cell[] line;
            int screenRow = logical - history.size();
            if (logical >= 0 && logical < history.size()) line = history.get(logical);
            else if (screenRow >= 0 && screenRow < modelRows) line = visible[screenRow];
            else continue;
            int count = Math.min(lastColumns, line.length);
            for (int column = 0; column < count; column++) {
                AnsiTerminalModel.Cell cell = line[column];
                int foreground = resolveColor(cell.foreground, DEFAULT_FOREGROUND);
                int background = resolveColor(cell.background, DEFAULT_BACKGROUND);
                if (cell.inverse) {
                    int swap = foreground;
                    foreground = background;
                    background = swap;
                }
                float left = getPaddingLeft() + column * cellWidth;
                float top = getPaddingTop() + viewRow * cellHeight;
                if (background != DEFAULT_BACKGROUND) {
                    backgroundPaint.setColor(background);
                    canvas.drawRect(left, top, left + cellWidth, top + cellHeight, backgroundPaint);
                }
                if (drawCursor && screenRow == cursorRow && column == Math.min(cursorColumn, lastColumns - 1)) {
                    backgroundPaint.setColor(CURSOR_COLOR);
                    canvas.drawRect(left, top, left + cellWidth, top + cellHeight, backgroundPaint);
                    foreground = DEFAULT_BACKGROUND;
                }
                if (cell.wideContinuation || cell.text.trim().isEmpty()) continue;
                textPaint.setColor(foreground);
                textPaint.setFakeBoldText(cell.bold);
                textPaint.setTextSkewX(cell.italic ? -0.20f : 0f);
                textPaint.setUnderlineText(cell.underline);
                textPaint.setStrikeThruText(cell.crossedOut);
                textPaint.setAlpha(cell.faint ? 150 : 255);
                canvas.drawText(cell.text, left, top + baselineOffset, textPaint);
            }
        }
        textPaint.setFakeBoldText(false);
        textPaint.setTextSkewX(0f);
        textPaint.setUnderlineText(false);
        textPaint.setStrikeThruText(false);
        textPaint.setAlpha(255);
    }

    private static int resolveColor(int value, int fallback) {
        if (value == AnsiTerminalModel.DEFAULT_COLOR) return fallback;
        int rgb = (value & 0x1000000) != 0
                ? value & 0xFFFFFF
                : AnsiTerminalModel.paletteColor(value);
        return Color.rgb((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF);
    }

    @Override
    public boolean onTouchEvent(MotionEvent event) {
        if (event.getActionMasked() == MotionEvent.ACTION_DOWN) {
            requestFocus();
            scrollToBottom();
            // A terminal is an editor as well as a canvas. Requesting the IME
            // here lets an observer type directly without using the command
            // bar, while the bridge still decides whether input takes control.
            post(() -> {
                InputMethodManager manager = (InputMethodManager)
                        getContext().getSystemService(Context.INPUT_METHOD_SERVICE);
                if (manager != null) {
                    manager.restartInput(this);
                    manager.showSoftInput(this, InputMethodManager.SHOW_IMPLICIT);
                }
            });
            return true;
        }
        if (event.getActionMasked() == MotionEvent.ACTION_UP) {
            performClick();
            return true;
        }
        return super.onTouchEvent(event);
    }

    @Override
    public boolean performClick() {
        super.performClick();
        return true;
    }

    @Override
    public boolean onCheckIsTextEditor() {
        return true;
    }

    @Override
    public InputConnection onCreateInputConnection(EditorInfo outAttrs) {
        outAttrs.inputType = InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
                | InputType.TYPE_TEXT_VARIATION_NORMAL;
        outAttrs.imeOptions = EditorInfo.IME_FLAG_NO_EXTRACT_UI;
        return new TerminalInputConnection(this);
    }

    /** InputConnection that emits only committed text to the PTY. */
    private final class TerminalInputConnection extends BaseInputConnection {
        TerminalInputConnection(View target) {
            // The terminal has no editable document; composition is tracked
            // explicitly so unfinished Pinyin never reaches the shell.
            super(target, false);
        }

        @Override
        public boolean setComposingText(CharSequence text, int newCursorPosition) {
            composer.set(text);
            invalidate();
            return true;
        }

        @Override
        public boolean commitText(CharSequence text, int newCursorPosition) {
            byte[] committed = composer.commit(text);
            if (committed.length > 0) emit(committed);
            invalidate();
            return true;
        }

        @Override
        public boolean finishComposingText() {
            // IMEs normally follow this with commitText. Do not leak a
            // transient composition when an IME merely cancels its editor.
            composer.clear();
            invalidate();
            return true;
        }

        @Override
        public boolean deleteSurroundingText(int beforeLength, int afterLength) {
            if (composer.deleteLastCodePoint()) {
                invalidate();
                return true;
            }
            int count = Math.max(beforeLength, afterLength);
            if (count <= 0) return true;
            for (int i = 0; i < count; i++) emit(TerminalInputCodec.key(
                    TerminalInputCodec.Key.BACKSPACE));
            return true;
        }

        @Override
        public boolean deleteSurroundingTextInCodePoints(int beforeLength, int afterLength) {
            return deleteSurroundingText(beforeLength, afterLength);
        }

        @Override
        public boolean sendKeyEvent(KeyEvent event) {
            if (event == null || event.getAction() != KeyEvent.ACTION_DOWN) return true;
            byte[] sequence = keySequence(event.getKeyCode(), event);
            if (sequence != null) emit(sequence);
            else {
                int unicode = event.getUnicodeChar();
                if (unicode > 0) emit(TerminalInputCodec.utf8(
                        new String(Character.toChars(unicode))));
            }
            return true;
        }

        @Override
        public boolean performEditorAction(int actionCode) {
            if (actionCode == EditorInfo.IME_ACTION_DONE
                    || actionCode == EditorInfo.IME_ACTION_GO
                    || actionCode == EditorInfo.IME_ACTION_SEND
                    || actionCode == EditorInfo.IME_ACTION_NEXT) {
                emit(TerminalInputCodec.key(TerminalInputCodec.Key.ENTER));
                return true;
            }
            return super.performEditorAction(actionCode);
        }

        @Override
        public boolean commitCompletion(android.view.inputmethod.CompletionInfo text) {
            return text != null && commitText(text.getText(), 1);
        }

        private void emit(byte[] bytes) {
            if (listener != null && bytes != null && bytes.length > 0) listener.onInput(bytes);
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        byte[] sequence = keySequence(keyCode, event);
        if (sequence != null) {
            if (listener != null) listener.onInput(sequence);
            return true;
        }
        int unicode = event.getUnicodeChar();
        if (unicode > 0) {
            String value = new String(Character.toChars(unicode));
            if (listener != null) listener.onInput(TerminalInputCodec.utf8(value));
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private static byte[] keySequence(int keyCode, KeyEvent event) {
        if (event.isCtrlPressed() && keyCode >= KeyEvent.KEYCODE_A && keyCode <= KeyEvent.KEYCODE_Z) {
            return TerminalInputCodec.ctrl((char) ('A' + keyCode - KeyEvent.KEYCODE_A));
        }
        TerminalInputCodec.Key key = null;
        switch (keyCode) {
            case KeyEvent.KEYCODE_ESCAPE: key = TerminalInputCodec.Key.ESCAPE; break;
            case KeyEvent.KEYCODE_TAB: key = TerminalInputCodec.Key.TAB; break;
            case KeyEvent.KEYCODE_ENTER: key = TerminalInputCodec.Key.ENTER; break;
            case KeyEvent.KEYCODE_DEL: key = TerminalInputCodec.Key.BACKSPACE; break;
            case KeyEvent.KEYCODE_DPAD_UP: key = TerminalInputCodec.Key.UP; break;
            case KeyEvent.KEYCODE_DPAD_DOWN: key = TerminalInputCodec.Key.DOWN; break;
            case KeyEvent.KEYCODE_DPAD_RIGHT: key = TerminalInputCodec.Key.RIGHT; break;
            case KeyEvent.KEYCODE_DPAD_LEFT: key = TerminalInputCodec.Key.LEFT; break;
            case KeyEvent.KEYCODE_MOVE_HOME: key = TerminalInputCodec.Key.HOME; break;
            case KeyEvent.KEYCODE_MOVE_END: key = TerminalInputCodec.Key.END; break;
            case KeyEvent.KEYCODE_PAGE_UP: key = TerminalInputCodec.Key.PAGE_UP; break;
            case KeyEvent.KEYCODE_PAGE_DOWN: key = TerminalInputCodec.Key.PAGE_DOWN; break;
            default: break;
        }
        return key == null ? null : TerminalInputCodec.key(key);
    }

    @Override
    protected void onDetachedFromWindow() {
        main.removeCallbacks(resizeCommit);
        super.onDetachedFromWindow();
    }

    private float sp(float value) {
        return value * getResources().getDisplayMetrics().scaledDensity;
    }
}
