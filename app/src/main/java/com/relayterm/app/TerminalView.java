package com.relayterm.app;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.util.AttributeSet;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;

import java.nio.charset.StandardCharsets;
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
    private AnsiTerminalModel model = new AnsiTerminalModel();
    private Listener listener;
    private float cellWidth;
    private float cellHeight;
    private float baselineOffset;
    private int lastColumns = 100;
    private int lastRows = 32;
    private int scrollOffset;

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
        if (columns != lastColumns || rows != lastRows) {
            lastColumns = columns;
            lastRows = rows;
            model.resize(columns, rows);
            if (listener != null) listener.onResize(columns, rows);
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
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        byte[] sequence = keySequence(keyCode, event);
        if (sequence != null) {
            if (listener != null) listener.onInput(sequence);
            return true;
        }
        int unicode = event.getUnicodeChar();
        if (unicode > 0) {
            String value = new String(Character.toChars(unicode));
            if (listener != null) listener.onInput(value.getBytes(StandardCharsets.UTF_8));
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private static byte[] keySequence(int keyCode, KeyEvent event) {
        if (event.isCtrlPressed() && keyCode >= KeyEvent.KEYCODE_A && keyCode <= KeyEvent.KEYCODE_Z) {
            return new byte[]{(byte) (keyCode - KeyEvent.KEYCODE_A + 1)};
        }
        String value = null;
        switch (keyCode) {
            case KeyEvent.KEYCODE_ESCAPE: value = "\u001b"; break;
            case KeyEvent.KEYCODE_TAB: value = "\t"; break;
            case KeyEvent.KEYCODE_ENTER: value = "\r"; break;
            case KeyEvent.KEYCODE_DEL: value = "\u007f"; break;
            case KeyEvent.KEYCODE_DPAD_UP: value = "\u001b[A"; break;
            case KeyEvent.KEYCODE_DPAD_DOWN: value = "\u001b[B"; break;
            case KeyEvent.KEYCODE_DPAD_RIGHT: value = "\u001b[C"; break;
            case KeyEvent.KEYCODE_DPAD_LEFT: value = "\u001b[D"; break;
            case KeyEvent.KEYCODE_MOVE_HOME: value = "\u001b[H"; break;
            case KeyEvent.KEYCODE_MOVE_END: value = "\u001b[F"; break;
            case KeyEvent.KEYCODE_PAGE_UP: value = "\u001b[5~"; break;
            case KeyEvent.KEYCODE_PAGE_DOWN: value = "\u001b[6~"; break;
            default: break;
        }
        return value == null ? null : value.getBytes(StandardCharsets.UTF_8);
    }

    private float sp(float value) {
        return value * getResources().getDisplayMetrics().scaledDensity;
    }
}
