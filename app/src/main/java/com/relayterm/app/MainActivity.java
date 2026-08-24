package com.relayterm.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.Window;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.HorizontalScrollView;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Space;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsAnimationCompat;
import androidx.core.view.WindowInsetsCompat;

import com.google.zxing.client.android.Intents;
import com.journeyapps.barcodescanner.CaptureActivity;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Single-screen controller for bridge-owned, reconnectable PTY sessions. */
public final class MainActivity extends Activity {
    private static final int PAIRING_SCAN_REQUEST = 0x52A1;
    private static final int BG = Color.rgb(11, 16, 22);
    private static final int SURFACE = Color.rgb(17, 25, 35);
    private static final int RAISED = Color.rgb(24, 35, 46);
    private static final int OUTLINE = Color.rgb(43, 58, 71);
    private static final int PRIMARY = Color.rgb(234, 242, 244);
    private static final int SECONDARY = Color.rgb(154, 170, 179);
    private static final int ACCENT = Color.rgb(87, 214, 180);
    private static final int WARNING = Color.rgb(245, 190, 84);
    private static final int DANGER = Color.rgb(255, 125, 130);

    private TerminalStore store;
    private CommandRunner localRunner;
    private PtyClient ptyClient;
    private CatalogClient catalogClient;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<TerminalProfile> profiles = new ArrayList<>();
    private final Map<String, AnsiTerminalModel> terminals = new HashMap<>();
    private final Set<String> requestedProfiles = new HashSet<>();
    private final Set<String> readyProfiles = new HashSet<>();
    private final Set<String> endedProfiles = new HashSet<>();
    private final Set<String> pendingReplayProfiles = new HashSet<>();
    private final Map<String, String> profileErrors = new HashMap<>();
    private final Map<String, String> profileRoles = new HashMap<>();
    private final Map<String, JSONObject> catalogSessions = new HashMap<>();
    private final Set<String> syncingConnections = new HashSet<>();

    private Spinner profileSpinner;
    private TextView endpointView;
    private TextView statusView;
    private TerminalView terminalView;
    private EditText commandInput;
    private Button connectButton;
    private Button sendButton;
    private Button stopButton;
    private int selectedIndex;
    private String activePtyProfileId = "";
    private String activeLocalProfileId = "";
    private boolean foreground;
    private final StopFlow stopFlow = new StopFlow();
    private AlertDialog stopDialog;
    private int baseRootLeft;
    private int baseRootTop;
    private int baseRootRight;
    private int baseRootBottom;

    private final PtyClient.Listener ptyListener = new PtyClient.Listener() {
        @Override
        public void onConnecting(String profileId, int attempt) {
            readyProfiles.remove(profileId);
            if (isSelected(profileId)) {
                resetStopFlow();
                connectButton.setEnabled(false);
                setStatus(attempt == 1 ? "连接中…" : "重连中（" + attempt + "）…", SECONDARY);
            }
        }

        @Override
        public void onReady(String profileId, int pid, boolean resumed, String role) {
            readyProfiles.add(profileId);
            endedProfiles.remove(profileId);
            profileErrors.remove(profileId);
            requestedProfiles.add(profileId);
            profileRoles.put(profileId, role);
            if (resumed) pendingReplayProfiles.add(profileId);
            else pendingReplayProfiles.remove(profileId);
            resetStopFlow();
            if (isSelected(profileId)) {
                connectButton.setEnabled(true);
                connectButton.setText("重连");
                sendButton.setEnabled(true);
                stopButton.setEnabled(true);
                updateStopButtonStyle(false);
                String label = "controller".equals(role) ? "控制端" : "观察端";
                setStatus(resumed ? label + " · 已恢复" : label + " · 运行中", ACCENT);
            }
        }

        @Override
        public void onOutput(String profileId, byte[] bytes) {
            AnsiTerminalModel model = terminalFor(profileId);
            if (pendingReplayProfiles.remove(profileId)) {
                // The bridge sends a resumed snapshot before live output. Swap
                // it in one callback so reconnect/rotation never shows a
                // transient blank grid or duplicates the snapshot.
                model.reset();
                model.resize(terminalView.getTerminalColumns(), terminalView.getTerminalRows());
            }
            model.feed(bytes);
            if (isSelected(profileId)) {
                terminalView.scrollToBottom();
                terminalView.invalidate();
            }
        }

        @Override
        public void onEvent(String profileId, JSONObject event) {
            String type = event.optString("type", "");
            if ("resync_required".equals(type)) {
                pendingReplayProfiles.remove(profileId);
                terminalFor(profileId).reset();
                if (isSelected(profileId)) setStatus("需要重新同步", WARNING);
            } else if ("control_changed".equals(type)) {
                String role = event.optString("role", "observer");
                profileRoles.put(profileId, role);
                if (isSelected(profileId)) {
                    setStatus("controller".equals(role) ? "控制端 · 运行中" : "观察端 · 运行中",
                            "controller".equals(role) ? ACCENT : SECONDARY);
                }
            }
        }

        @Override
        public void onExit(String profileId, int code, String cwd) {
            readyProfiles.remove(profileId);
            endedProfiles.add(profileId);
            pendingReplayProfiles.remove(profileId);
            resetStopFlow();
            appendSystem(profileId, "进程已退出（" + code + "）"
                    + (cwd.isEmpty() ? "" : " · " + cwd) + "\r\n");
            if (isSelected(profileId)) {
                connectButton.setEnabled(true);
                connectButton.setText("新建");
                sendButton.setEnabled(false);
                stopButton.setEnabled(false);
                updateStopButtonStyle(false);
                setStatus("已退出 · " + code, code == 0 ? SECONDARY : DANGER);
            }
        }

        @Override
        public void onDisconnected(String profileId, boolean reconnecting) {
            readyProfiles.remove(profileId);
            if (isSelected(profileId)) resetStopFlow();
            if (!reconnecting) pendingReplayProfiles.remove(profileId);
            if (isSelected(profileId) && reconnecting) {
                connectButton.setEnabled(true);
                setStatus("重连中…", SECONDARY);
            }
        }

        @Override
        public void onError(String profileId, String message) {
            profileErrors.put(profileId, message);
            if (isSelected(profileId)) {
                resetStopFlow();
                connectButton.setEnabled(true);
                setStatus(message == null || message.isEmpty() ? "连接错误" : message, DANGER);
            }
        }
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        getWindow().setStatusBarColor(BG);
        getWindow().setNavigationBarColor(BG);
        if (android.os.Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(true);
        store = new TerminalStore(this);
        localRunner = new CommandRunner();
        ptyClient = new PtyClient(this);
        catalogClient = new CatalogClient();
        profiles.addAll(store.load());
        selectedIndex = findSelectedIndex(store.selectedId());
        setContentView(buildScreen());
        refreshProfileSpinner();
        selectProfile(selectedIndex, false);
        handlePairingIntent(getIntent());
        syncCatalogs();
        if (state != null && state.getBoolean("resumePty", false)) {
            requestedProfiles.add(profiles.get(selectedIndex).id);
            main.post(this::connectSelected);
        }
    }

    private View buildScreen() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        baseRootLeft = dp(12);
        baseRootTop = dp(6);
        baseRootRight = dp(12);
        baseRootBottom = dp(8);
        root.setPadding(baseRootLeft, baseRootTop, baseRootRight, baseRootBottom);
        root.setBackgroundColor(BG);
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
            Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars());
            Insets ime = insets.getInsets(WindowInsetsCompat.Type.ime());
            // ADJUST_RESIZE already changes the root's measured height. Use
            // the larger bottom inset once instead of adding nav + IME.
            int bottom = Math.max(bars.bottom, ime.bottom);
            int left = Math.max(0, bars.left);
            int right = Math.max(0, bars.right);
            int top = Math.max(0, bars.top);
            int nextLeft = baseRootLeft + left;
            int nextTop = baseRootTop + top;
            int nextRight = baseRootRight + right;
            int nextBottom = baseRootBottom + bottom;
            if (view.getPaddingLeft() != nextLeft || view.getPaddingTop() != nextTop
                    || view.getPaddingRight() != nextRight || view.getPaddingBottom() != nextBottom) {
                view.setPadding(nextLeft, nextTop, nextRight, nextBottom);
            }
            return insets;
        });
        ViewCompat.setWindowInsetsAnimationCallback(root,
                new WindowInsetsAnimationCompat.Callback(
                        WindowInsetsAnimationCompat.Callback.DISPATCH_MODE_CONTINUE_ON_SUBTREE) {
                    private int imeAnimations;

                    @Override
                    public void onPrepare(WindowInsetsAnimationCompat animation) {
                        if ((animation.getTypeMask() & WindowInsetsCompat.Type.ime()) != 0) {
                            imeAnimations++;
                            if (terminalView != null) terminalView.setResizeSuspended(true);
                        }
                    }

                    @Override
                    public WindowInsetsCompat onProgress(WindowInsetsCompat insets,
                            List<WindowInsetsAnimationCompat> runningAnimations) {
                        return insets;
                    }

                    @Override
                    public void onEnd(WindowInsetsAnimationCompat animation) {
                        if ((animation.getTypeMask() & WindowInsetsCompat.Type.ime()) != 0) {
                            imeAnimations = Math.max(0, imeAnimations - 1);
                            if (imeAnimations == 0 && terminalView != null) {
                                terminalView.setResizeSuspended(false);
                            }
                        }
                    }
                });
        ViewCompat.requestApplyInsets(root);

        FrameLayout header = new FrameLayout(this);
        header.setLayoutParams(new LinearLayout.LayoutParams(-1, dp(54)));
        LinearLayout heading = new LinearLayout(this);
        heading.setOrientation(LinearLayout.VERTICAL);
        TextView title = label("RelayTerm", 20, PRIMARY);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        TextView subtitle = label("交互式终端", 11, SECONDARY);
        heading.addView(title);
        heading.addView(subtitle);
        header.addView(heading, new FrameLayout.LayoutParams(-2, -2, Gravity.CENTER_VERTICAL));
        LinearLayout headerActions = row();
        ImageButton scan = iconButton(android.R.drawable.ic_menu_camera, "扫码配对");
        scan.setOnClickListener(v -> startPairingScan());
        headerActions.addView(scan, new LinearLayout.LayoutParams(dp(42), dp(42)));
        ImageButton add = iconButton(android.R.drawable.ic_input_add, "新增终端");
        add.setOnClickListener(v -> showEditor(null));
        LinearLayout.LayoutParams addParams = new LinearLayout.LayoutParams(dp(42), dp(42));
        addParams.setMarginStart(dp(6));
        headerActions.addView(add, addParams);
        header.addView(headerActions,
                new FrameLayout.LayoutParams(dp(90), dp(42), Gravity.END | Gravity.CENTER_VERTICAL));
        root.addView(header);

        LinearLayout switchRow = row();
        profileSpinner = new Spinner(this);
        profileSpinner.setBackground(buttonBackground(6));
        switchRow.addView(profileSpinner, new LinearLayout.LayoutParams(0, dp(46), 1));
        ImageButton edit = iconButton(android.R.drawable.ic_menu_edit, "编辑终端");
        edit.setOnClickListener(v -> { if (!profiles.isEmpty()) showEditor(profiles.get(selectedIndex)); });
        LinearLayout.LayoutParams editParams = new LinearLayout.LayoutParams(dp(42), dp(46));
        editParams.setMarginStart(dp(6));
        switchRow.addView(edit, editParams);
        connectButton = actionButton("连接", android.R.drawable.ic_menu_share);
        connectButton.setOnClickListener(v -> connectSelected());
        LinearLayout.LayoutParams connectParams = new LinearLayout.LayoutParams(dp(86), dp(46));
        connectParams.setMarginStart(dp(6));
        switchRow.addView(connectButton, connectParams);
        root.addView(switchRow, new LinearLayout.LayoutParams(-1, dp(50)));

        endpointView = label("", 11, SECONDARY);
        endpointView.setSingleLine(true);
        endpointView.setEllipsize(android.text.TextUtils.TruncateAt.MIDDLE);
        endpointView.setPadding(dp(4), 0, dp(4), dp(4));
        root.addView(endpointView, new LinearLayout.LayoutParams(-1, dp(23)));

        LinearLayout statusRow = row();
        statusView = label("未连接", 12, SECONDARY);
        statusView.setTypeface(Typeface.MONOSPACE, Typeface.BOLD);
        statusView.setSingleLine(true);
        statusView.setEllipsize(android.text.TextUtils.TruncateAt.END);
        statusRow.addView(statusView, new LinearLayout.LayoutParams(0, dp(25), 1));
        ImageButton securityInfo = iconButton(android.R.drawable.ic_lock_lock,
                "远端使用 HTTPS，token 由系统密钥库保护");
        securityInfo.setTooltipText("远端使用 HTTPS，token 由系统密钥库保护");
        statusRow.addView(securityInfo, new LinearLayout.LayoutParams(dp(40), dp(25)));
        ImageButton keyboard = iconButton(android.R.drawable.ic_menu_edit, "打开输入法");
        keyboard.setOnClickListener(v -> showTerminalInputMethod());
        LinearLayout.LayoutParams keyboardParams = new LinearLayout.LayoutParams(dp(40), dp(25));
        keyboardParams.setMarginStart(dp(4));
        statusRow.addView(keyboard, keyboardParams);
        root.addView(statusRow);

        FrameLayout terminalFrame = new FrameLayout(this);
        terminalFrame.setBackground(panelBackground(SURFACE, OUTLINE, 4));
        terminalFrame.setPadding(dp(7), dp(5), dp(7), dp(5));
        terminalView = new TerminalView(this);
        terminalView.setListener(new TerminalView.Listener() {
            @Override public void onResize(int columns, int rows) {
                if (ptyClient.isConnected()) ptyClient.resize(columns, rows);
            }
            @Override public void onInput(byte[] bytes) { sendRaw(bytes); }
        });
        terminalFrame.addView(terminalView, new FrameLayout.LayoutParams(-1, -1));
        root.addView(terminalFrame, new LinearLayout.LayoutParams(-1, 0, 1));

        root.addView(buildControlBar(), new LinearLayout.LayoutParams(-1, dp(44)));

        LinearLayout commandRow = row();
        commandInput = new EditText(this);
        commandInput.setSingleLine(true);
        commandInput.setTextColor(PRIMARY);
        commandInput.setHintTextColor(SECONDARY);
        commandInput.setHint("输入");
        commandInput.setTextSize(14);
        commandInput.setTypeface(Typeface.MONOSPACE);
        commandInput.setPadding(dp(10), 0, dp(10), 0);
        commandInput.setInputType(InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
                | InputType.TYPE_TEXT_VARIATION_NORMAL);
        commandInput.setImeOptions(EditorInfo.IME_ACTION_SEND);
        commandInput.setOnEditorActionListener((v, action, event) -> {
            boolean enter = event != null && event.getKeyCode() == KeyEvent.KEYCODE_ENTER
                    && event.getAction() == KeyEvent.ACTION_DOWN;
            if (action == EditorInfo.IME_ACTION_SEND || enter) {
                sendCommand();
                return true;
            }
            return false;
        });
        commandInput.setBackground(panelBackground(RAISED, OUTLINE, 6));
        commandRow.addView(commandInput, new LinearLayout.LayoutParams(0, dp(48), 1));
        sendButton = actionButton("发送", android.R.drawable.ic_media_play);
        sendButton.setOnClickListener(v -> sendCommand());
        LinearLayout.LayoutParams sendParams = new LinearLayout.LayoutParams(dp(78), dp(48));
        sendParams.setMarginStart(dp(6));
        commandRow.addView(sendButton, sendParams);
        stopButton = actionButton("停止", android.R.drawable.ic_media_pause);
        updateStopButtonStyle(false);
        stopButton.setEnabled(false);
        stopButton.setOnClickListener(v -> stopCurrent());
        LinearLayout.LayoutParams stopParams = new LinearLayout.LayoutParams(dp(108), dp(48));
        stopParams.setMarginStart(dp(6));
        commandRow.addView(stopButton, stopParams);
        root.addView(commandRow, new LinearLayout.LayoutParams(-1, dp(54)));

        LinearLayout footer = row();
        Button clear = actionButton("清空", android.R.drawable.ic_menu_close_clear_cancel);
        clear.setOnClickListener(v -> clearTerminal());
        footer.addView(clear, new LinearLayout.LayoutParams(dp(90), dp(34)));
        Space space = new Space(this);
        footer.addView(space, new LinearLayout.LayoutParams(0, dp(34), 1));
        root.addView(footer, new LinearLayout.LayoutParams(-1, dp(36)));

        profileSpinner.setOnItemSelectedListener(new android.widget.AdapterView.OnItemSelectedListener() {
            @Override public void onItemSelected(android.widget.AdapterView<?> parent, View view, int position, long id) {
                if (position >= 0 && position < profiles.size() && position != selectedIndex) {
                    selectProfile(position, true);
                }
            }
            @Override public void onNothingSelected(android.widget.AdapterView<?> parent) { }
        });
        return root;
    }

    private View buildControlBar() {
        HorizontalScrollView scroll = new HorizontalScrollView(this);
        scroll.setHorizontalScrollBarEnabled(false);
        scroll.setFillViewport(false);
        LinearLayout controls = row();
        addControl(controls, "Esc", "Escape", "\u001b");
        addControl(controls, "Tab", "Tab", "\t");
        addControl(controls, "C-C", "Ctrl-C", null);
        addControl(controls, "C-D", "Ctrl-D", null);
        addControl(controls, "C-L", "Ctrl-L", "\u000c");
        addControl(controls, "↑", "上方向键", "\u001b[A");
        addControl(controls, "↓", "下方向键", "\u001b[B");
        addControl(controls, "←", "左方向键", "\u001b[D");
        addControl(controls, "→", "右方向键", "\u001b[C");
        addControl(controls, "Home", "Home", "\u001b[H");
        addControl(controls, "End", "End", "\u001b[F");
        addControl(controls, "PgUp", "Page Up", "\u001b[5~");
        addControl(controls, "PgDn", "Page Down", "\u001b[6~");
        scroll.addView(controls, new HorizontalScrollView.LayoutParams(-2, dp(42)));
        return scroll;
    }

    private void addControl(LinearLayout parent, String text, String description, String sequence) {
        Button button = actionButton(text, 0);
        button.setContentDescription(description);
        button.setTooltipText(description);
        button.setPadding(dp(7), 0, dp(7), 0);
        button.setOnClickListener(v -> {
            if ("Ctrl-C".equals(description)) sendSignal("INT");
            else if ("Ctrl-D".equals(description)) sendSignal("EOF");
            else if (sequence != null) sendRaw(sequence.getBytes(StandardCharsets.UTF_8));
        });
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                text.length() > 3 ? dp(58) : dp(48), dp(38));
        params.setMarginEnd(dp(4));
        parent.addView(button, params);
    }

    private void refreshProfileSpinner() {
        List<String> names = new ArrayList<>();
        for (TerminalProfile profile : profiles) {
            names.add(profile.name + (profile.managed ? " · PC" : ""));
        }
        ArrayAdapter<String> adapter = new ArrayAdapter<String>(this,
                android.R.layout.simple_spinner_item, names) {
            @Override public View getView(int position, View convertView, android.view.ViewGroup parent) {
                TextView view = (TextView) super.getView(position, convertView, parent);
                view.setTextColor(PRIMARY);
                view.setTextSize(15);
                view.setPadding(dp(10), 0, dp(6), 0);
                return view;
            }
            @Override public View getDropDownView(int position, View convertView, android.view.ViewGroup parent) {
                TextView view = (TextView) super.getDropDownView(position, convertView, parent);
                view.setTextColor(PRIMARY);
                view.setTextSize(15);
                view.setPadding(dp(12), dp(10), dp(8), dp(10));
                view.setBackgroundColor(RAISED);
                return view;
            }
        };
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        profileSpinner.setAdapter(adapter);
        if (!profiles.isEmpty()) profileSpinner.setSelection(
                Math.max(0, Math.min(selectedIndex, profiles.size() - 1)));
    }

    private void selectProfile(int index, boolean announce) {
        if (profiles.isEmpty()) return;
        selectedIndex = Math.max(0, Math.min(index, profiles.size() - 1));
        TerminalProfile selected = profiles.get(selectedIndex);
        if (!selected.id.equals(store.selectedId())) {
            resetStopFlow();
        }
        store.setSelectedId(selected.id);
        if (!activePtyProfileId.isEmpty() && !activePtyProfileId.equals(selected.id)) {
            resetStopFlow();
            ptyClient.disconnect();
            activePtyProfileId = "";
        }
        endpointView.setText(selected.endpoint);
        terminalView.setModel(terminalFor(selected.id));
        updateSelectedState();
        if (announce) toast("已切换到 " + selected.name);
        if (foreground && requestedProfiles.contains(selected.id) && !endedProfiles.contains(selected.id)
                && !selected.isLocal()
                && !selected.id.equals(activePtyProfileId)) {
            main.post(() -> openPty(selected, true));
        }
    }

    private void updateSelectedState() {
        if (profiles.isEmpty()) return;
        TerminalProfile profile = profiles.get(selectedIndex);
        connectButton.setEnabled(true);
        if (profile.isLocal()) {
            boolean ready = readyProfiles.contains(profile.id);
            connectButton.setText(ready ? "就绪" : "连接");
            sendButton.setEnabled(ready && !localRunner.isRunning());
            stopButton.setEnabled(localRunner.isRunning());
            updateStopButtonStyle(stopFlow.isArmed());
            setStatus(ready ? "本机就绪" : "未连接", ready ? ACCENT : SECONDARY);
            return;
        }
        if (endedProfiles.contains(profile.id)) {
            connectButton.setText("新建");
            sendButton.setEnabled(false);
            stopButton.setEnabled(false);
            updateStopButtonStyle(false);
            setStatus("已退出", SECONDARY);
        } else if (readyProfiles.contains(profile.id) && profile.id.equals(activePtyProfileId)) {
            connectButton.setText("重连");
            sendButton.setEnabled(true);
            stopButton.setEnabled(true);
            updateStopButtonStyle(stopFlow.isArmed());
            String role = profileRoles.getOrDefault(profile.id, "observer");
            setStatus("controller".equals(role) ? "控制端 · 运行中" : "观察端 · 运行中",
                    "controller".equals(role) ? ACCENT : SECONDARY);
        } else if (profileErrors.containsKey(profile.id)) {
            connectButton.setText("重连");
            sendButton.setEnabled(false);
            stopButton.setEnabled(false);
            updateStopButtonStyle(false);
            setStatus(profileErrors.get(profile.id), DANGER);
        } else {
            connectButton.setText(requestedProfiles.contains(profile.id) ? "重连" : "连接");
            sendButton.setEnabled(false);
            stopButton.setEnabled(false);
            updateStopButtonStyle(false);
            JSONObject session = catalogSessions.get(profile.id);
            if (session != null && session.optBoolean("running", false)) {
                setStatus("运行中 · 未连接", SECONDARY);
                connectButton.setText("连接");
            } else if (session != null && "exited".equals(session.optString("state", ""))) {
                setStatus("已退出", SECONDARY);
                connectButton.setText("新建");
                endedProfiles.add(profile.id);
            } else {
                setStatus(requestedProfiles.contains(profile.id) ? "会话已保留" : "未连接", SECONDARY);
            }
        }
    }

    private void connectSelected() {
        if (profiles.isEmpty()) return;
        TerminalProfile profile = profiles.get(selectedIndex);
        String endpointError = CommandPolicy.validateEndpoint(profile.endpoint);
        if (!endpointError.isEmpty()) {
            setStatus(endpointError, DANGER);
            return;
        }
        requestedProfiles.add(profile.id);
        profileErrors.remove(profile.id);
        if (profile.isLocal()) {
            readyProfiles.add(profile.id);
            appendSystem(profile.id, "本机演示终端已就绪\r\n");
            updateSelectedState();
            return;
        }
        boolean createFresh = endedProfiles.remove(profile.id);
        if (createFresh && activePtyProfileId.equals(profile.id)) {
            resetStopFlow();
            ptyClient.closeSession(true);
        }
        openPty(profile, !createFresh);
    }

    private void openPty(TerminalProfile profile, boolean resume) {
        if (!foreground && !isChangingConfigurations()) return;
        resetStopFlow();
        if (!activePtyProfileId.isEmpty() && !activePtyProfileId.equals(profile.id)) ptyClient.disconnect();
        activePtyProfileId = profile.id;
        ptyClient.connect(profile, terminalView.getTerminalColumns(), terminalView.getTerminalRows(),
                resume, ptyListener);
    }

    private void sendCommand() {
        if (profiles.isEmpty()) return;
        String command = commandInput.getText().toString();
        String validation = CommandPolicy.validate(command);
        if (!validation.isEmpty()) {
            toast(validation);
            return;
        }
        TerminalProfile profile = profiles.get(selectedIndex);
        if (profile.isLocal()) {
            runLocal(profile, command);
            commandInput.setText("");
            return;
        }
        if (!readyProfiles.contains(profile.id) || !profile.id.equals(activePtyProfileId)
                || !ptyClient.isConnected()) {
            toast("请先连接终端");
            return;
        }
        ptyClient.sendText(command);
        ptyClient.sendInput(new byte[]{'\r'});
        commandInput.setText("");
        terminalView.scrollToBottom();
    }

    private void showTerminalInputMethod() {
        if (terminalView == null) return;
        terminalView.requestFocus();
        terminalView.post(() -> {
            InputMethodManager manager = (InputMethodManager)
                    getSystemService(INPUT_METHOD_SERVICE);
            if (manager != null) {
                manager.restartInput(terminalView);
                manager.showSoftInput(terminalView, InputMethodManager.SHOW_IMPLICIT);
            }
        });
    }

    private void runLocal(TerminalProfile profile, String command) {
        if (!readyProfiles.contains(profile.id)) {
            toast("请先连接终端");
            return;
        }
        appendRaw(profile.id, ("$ " + command + "\r\n").getBytes(StandardCharsets.UTF_8));
        resetStopFlow();
        sendButton.setEnabled(false);
        stopButton.setEnabled(true);
        activeLocalProfileId = profile.id;
        setStatus("执行中…", SECONDARY);
        localRunner.run(profile, command, new CommandRunner.Callback() {
            @Override public void onStarted() { }
            @Override public void onFinished(CommandResult result) {
                if (!result.stdout.isEmpty()) appendRaw(profile.id,
                        (result.stdout + (result.stdout.endsWith("\n") ? "" : "\r\n"))
                                .getBytes(StandardCharsets.UTF_8));
                if (!result.stderr.isEmpty()) appendRaw(profile.id,
                        ("[stderr] " + result.stderr + "\r\n").getBytes(StandardCharsets.UTF_8));
                appendSystem(profile.id, "exit " + result.exitCode + "\r\n");
                activeLocalProfileId = "";
                resetStopFlow();
                if (isSelected(profile.id)) updateSelectedState();
            }
            @Override public void onError(String message) {
                appendSystem(profile.id, "error: " + message + "\r\n");
                activeLocalProfileId = "";
                resetStopFlow();
                if (isSelected(profile.id)) setStatus("执行失败", DANGER);
            }
        });
    }

    private void sendRaw(byte[] bytes) {
        if (profiles.isEmpty()) return;
        TerminalProfile profile = profiles.get(selectedIndex);
        if (profile.isLocal()) {
            toast("控制键用于远程 PTY");
            return;
        }
        if (!readyProfiles.contains(profile.id) || !ptyClient.isConnected()) {
            toast("终端尚未连接");
            return;
        }
        ptyClient.sendInput(bytes);
    }

    private void sendSignal(String name) {
        if (profiles.isEmpty()) return;
        TerminalProfile profile = profiles.get(selectedIndex);
        if (profile.isLocal()) {
            if ("INT".equals(name)) stopCurrent();
            return;
        }
        if (!readyProfiles.contains(profile.id) || !ptyClient.isConnected()) {
            toast("终端尚未连接");
            return;
        }
        ptyClient.signal(name);
    }

    private void stopCurrent() {
        if (profiles.isEmpty()) return;
        TerminalProfile profile = profiles.get(selectedIndex);
        boolean active = profile.isLocal()
                ? localRunner.isRunning() && profile.id.equals(activeLocalProfileId)
                : profile.id.equals(activePtyProfileId) && ptyClient.isConnected();
        StopFlow.Action action = stopFlow.click(active);
        if (action == StopFlow.Action.SHOW_STOP_CONFIRMATION) {
            showStopConfirmation(false, profile);
        } else if (action == StopFlow.Action.SHOW_FORCE_CONFIRMATION) {
            showStopConfirmation(true, profile);
        }
    }

    private void confirmStop(TerminalProfile profile) {
        StopFlow.Action action = stopFlow.confirmStop();
        if (action != StopFlow.Action.SEND_INTERRUPT) return;
        if (profile.isLocal()) {
            localRunner.cancel();
            activeLocalProfileId = "";
            appendSystem(profile.id, "已请求停止\r\n");
            resetStopFlow();
            updateSelectedState();
            return;
        }
        if (!profile.id.equals(activePtyProfileId) || !ptyClient.isConnected()) {
            resetStopFlow();
            updateSelectedState();
            return;
        }
        ptyClient.signal("INT");
        updateStopButtonStyle(true);
        setStatus("已发送 Ctrl-C", WARNING);
    }

    private void forceTerminate(String profileId) {
        stopFlow.confirmForce();
        ptyClient.closeSession(true);
        readyProfiles.remove(profileId);
        endedProfiles.add(profileId);
        requestedProfiles.remove(profileId);
        pendingReplayProfiles.remove(profileId);
        appendSystem(profileId, "会话已强制终止\r\n");
        resetStopFlow();
        if (isSelected(profileId)) updateSelectedState();
    }

    private void confirmForce(TerminalProfile profile) {
        if (!profile.isLocal() && profile.id.equals(activePtyProfileId)
                && ptyClient.isConnected()) forceTerminate(profile.id);
        else resetStopFlow();
    }

    private void showStopConfirmation(boolean force, TerminalProfile profile) {
        if (stopDialog != null && stopDialog.isShowing()) stopDialog.dismiss();
        String title = force ? "强制终止会话？" : "停止当前会话？";
        String message = force
                ? "这会立即关闭远程会话并丢失未保存状态。"
                : (profile.isLocal() ? "确认取消当前本机命令？" : "先向远程终端发送 Ctrl-C？");
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle(title)
                .setMessage(message)
                .setNegativeButton("取消", null)
                .setPositiveButton(force ? "强制终止" : "停止", null)
                .create();
        stopDialog = dialog;
        dialog.setOnShowListener(ignored -> {
            styleDialog(dialog, force);
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(view -> {
                stopDialog = null;
                dialog.dismiss();
                if (force) confirmForce(profile);
                else confirmStop(profile);
            });
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).setOnClickListener(view -> {
                stopDialog = null;
                dialog.dismiss();
                stopFlow.cancelConfirmation();
                updateStopButtonStyle(stopFlow.isArmed());
            });
        });
        dialog.setOnCancelListener(ignored -> {
            if (stopDialog == dialog) stopDialog = null;
            stopFlow.cancelConfirmation();
            updateStopButtonStyle(stopFlow.isArmed());
        });
        dialog.show();
    }

    private void resetStopFlow() {
        AlertDialog dialog = stopDialog;
        stopDialog = null;
        if (dialog != null && dialog.isShowing()) dialog.dismiss();
        stopFlow.reset();
        if (stopButton != null) updateStopButtonStyle(false);
    }

    private void updateStopButtonStyle(boolean force) {
        if (stopButton == null) return;
        stopButton.setText(force ? "强制终止" : "停止");
        stopButton.setTextSize(force ? 11 : 12);
        stopButton.setCompoundDrawablesWithIntrinsicBounds(
                force ? android.R.drawable.ic_menu_close_clear_cancel
                        : android.R.drawable.ic_media_pause,
                0, 0, 0);
        stopButton.setCompoundDrawablePadding(dp(3));
        int activeColor = force ? DANGER : WARNING;
        stopButton.setTextColor(new ColorStateList(
                new int[][]{{-android.R.attr.state_enabled}, {android.R.attr.state_pressed}, {}},
                new int[]{SECONDARY, activeColor, activeColor}));
    }

    private void clearTerminal() {
        if (profiles.isEmpty()) return;
        AnsiTerminalModel model = terminalFor(profiles.get(selectedIndex).id);
        model.reset();
        model.resize(terminalView.getTerminalColumns(), terminalView.getTerminalRows());
        terminalView.invalidate();
    }

    private AnsiTerminalModel terminalFor(String profileId) {
        AnsiTerminalModel existing = terminals.get(profileId);
        if (existing != null) return existing;
        AnsiTerminalModel created = new AnsiTerminalModel(
                terminalView == null ? 100 : terminalView.getTerminalColumns(),
                terminalView == null ? 32 : terminalView.getTerminalRows());
        terminals.put(profileId, created);
        return created;
    }

    private void appendSystem(String profileId, String message) {
        appendRaw(profileId, ("\u001b[38;5;244m[system] " + message + "\u001b[0m")
                .getBytes(StandardCharsets.UTF_8));
    }

    private void appendRaw(String profileId, byte[] bytes) {
        terminalFor(profileId).feed(bytes);
        if (isSelected(profileId)) terminalView.invalidate();
    }

    private void showEditor(TerminalProfile existing) {
        if (existing != null && existing.managed) {
            showManagedProfile(existing);
            return;
        }
        boolean creating = existing == null;
        LinearLayout form = new LinearLayout(this);
        form.setOrientation(LinearLayout.VERTICAL);
        form.setPadding(dp(20), dp(4), dp(20), dp(8));
        EditText name = field("名称", creating ? "开发机" : existing.name, false);
        EditText endpoint = field("地址", creating ? "https://HOST" : existing.endpoint, false);
        EditText token = field("Token（可选）", creating ? "" : existing.token, true);
        EditText startup = field("启动命令", creating ? "codex" : existing.startupCommand, false);
        EditText cwd = field("工作目录（可空）", creating ? "" : existing.workingDirectory, false);
        form.addView(name);
        form.addView(endpoint);
        form.addView(token);
        form.addView(startup);
        form.addView(cwd);
        ScrollView formScroll = new ScrollView(this);
        formScroll.addView(form, new ScrollView.LayoutParams(-1, -2));
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle(creating ? "新增终端" : "编辑终端")
                .setView(formScroll)
                .setNegativeButton("取消", null)
                .setPositiveButton("保存", null)
                .create();
        dialog.setOnShowListener(d -> {
            styleDialog(dialog, false);
            if (!creating) {
                dialog.setButton(AlertDialog.BUTTON_NEUTRAL, "删除", (view, which) -> confirmDelete(existing));
                dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setTextColor(WARNING);
            }
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
                String nameValue = name.getText().toString().trim();
                String endpointValue = endpoint.getText().toString().trim();
                String startupValue = startup.getText().toString().trim();
                String error = CommandPolicy.validateEndpoint(endpointValue);
                if (startupValue.isEmpty()) error = "请输入启动命令";
                if (startupValue.length() > 8192) error = "启动命令过长";
                if (nameValue.isEmpty()) error = "请输入名称";
                if (!error.isEmpty()) {
                    Toast.makeText(this, error, Toast.LENGTH_SHORT).show();
                    return;
                }
                String id = creating ? store.newId() : existing.id;
                TerminalProfile saved = new TerminalProfile(id, nameValue, endpointValue,
                        token.getText().toString(), startupValue, cwd.getText().toString());
                if (!creating && id.equals(activePtyProfileId)) {
                    resetStopFlow();
                    ptyClient.disconnect();
                    activePtyProfileId = "";
                    readyProfiles.remove(id);
                }
                store.upsert(saved);
                reloadProfiles(id);
                syncCatalogs();
                dialog.dismiss();
            });
        });
        dialog.show();
    }

    private void showManagedProfile(TerminalProfile profile) {
        String detail = profile.workingDirectory + "\n"
                + profile.shell + (profile.startupCommand.isEmpty() ? "" : " · " + profile.startupCommand);
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle(profile.name)
                .setMessage(detail)
                .setPositiveButton("关闭", null)
                .create();
        dialog.setOnShowListener(ignored -> styleDialog(dialog, false));
        dialog.show();
    }

    private void confirmDelete(TerminalProfile profile) {
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("删除终端？")
                .setMessage(profile.name)
                .setNegativeButton("取消", null)
                .setPositiveButton("删除", (ignored, which) -> {
                    Set<String> removed = new HashSet<>();
                    removed.add(profile.id);
                    for (TerminalProfile item : profiles) {
                        if (item.managed && item.connectionId.equals(profile.id)) {
                            removed.add(item.id);
                        }
                    }
                    if (removed.contains(activePtyProfileId)) {
                        resetStopFlow();
                        ptyClient.closeSession(true);
                        activePtyProfileId = "";
                    }
                    if (removed.contains(activeLocalProfileId)) {
                        resetStopFlow();
                        localRunner.cancel();
                        activeLocalProfileId = "";
                    }
                    store.remove(profile.id);
                    for (String id : removed) {
                        terminals.remove(id);
                        catalogSessions.remove(id);
                        requestedProfiles.remove(id);
                        readyProfiles.remove(id);
                        endedProfiles.remove(id);
                        pendingReplayProfiles.remove(id);
                        profileErrors.remove(id);
                        profileRoles.remove(id);
                    }
                    reloadProfiles("");
                }).create();
        dialog.setOnShowListener(ignored -> styleDialog(dialog, true));
        dialog.show();
    }

    private void reloadProfiles(String selectedId) {
        profiles.clear();
        profiles.addAll(store.load());
        if (profiles.isEmpty()) {
            selectedIndex = 0;
        } else {
            int requested = selectedId.isEmpty() ? selectedIndex : findIndexById(selectedId);
            selectedIndex = Math.max(0, Math.min(requested, profiles.size() - 1));
        }
        refreshProfileSpinner();
        selectProfile(selectedIndex, false);
    }

    private boolean isCurrentConnection(PcConnection expected) {
        for (TerminalProfile profile : store.manualConnections()) {
            if (profile.id.equals(expected.id)) {
                return profile.endpoint.equals(expected.endpoint) && profile.token.equals(expected.token);
            }
        }
        return false;
    }

    private void syncCatalogs() {
        if (catalogClient == null) return;
        for (TerminalProfile source : store.manualConnections()) {
            if (!syncingConnections.add(source.id)) continue;
            PcConnection connection = PcConnection.fromManualProfile(source);
            catalogClient.sync(connection, new CatalogClient.Callback() {
                @Override
                public void onCatalog(PcConnection synced, List<TerminalProfile> managed,
                                      Map<String, JSONObject> sessions) {
                    syncingConnections.remove(synced.id);
                    if (!isCurrentConnection(synced)) {
                        syncCatalogs();
                        return;
                    }
                    String selectedId = profiles.isEmpty() || selectedIndex < 0
                            || selectedIndex >= profiles.size() ? "" : profiles.get(selectedIndex).id;
                    Set<String> previousManaged = new HashSet<>();
                    Set<String> currentManaged = new HashSet<>();
                    for (TerminalProfile item : profiles) {
                        if (item.managed && item.connectionId.equals(synced.id)) {
                            previousManaged.add(item.id);
                        }
                    }
                    for (TerminalProfile item : managed) currentManaged.add(item.id);
                    store.replaceManaged(synced.id, managed);
                    for (String id : previousManaged) catalogSessions.remove(id);
                    for (TerminalProfile item : managed) {
                        JSONObject session = sessions.get(item.remoteProfileId);
                        if (session == null) catalogSessions.remove(item.id);
                        else catalogSessions.put(item.id, session);
                    }
                    previousManaged.removeAll(currentManaged);
                    for (String removed : previousManaged) {
                        terminals.remove(removed);
                        requestedProfiles.remove(removed);
                        readyProfiles.remove(removed);
                        endedProfiles.remove(removed);
                        pendingReplayProfiles.remove(removed);
                        profileErrors.remove(removed);
                        profileRoles.remove(removed);
                    }
                    reloadProfiles(selectedId);
                }

                @Override
                public void onError(PcConnection synced, String message) {
                    syncingConnections.remove(synced.id);
                    if (!isCurrentConnection(synced)) syncCatalogs();
                    // Cached managed profiles remain available while the PC is offline.
                }
            });
        }
    }

    private void startPairingScan() {
        Intent scan = new Intent(this, CaptureActivity.class);
        scan.setAction(Intents.Scan.ACTION);
        scan.putExtra(Intents.Scan.FORMATS, "QR_CODE");
        scan.putExtra(Intents.Scan.PROMPT_MESSAGE, "扫描电脑端显示的 RelayTerm 配对二维码");
        scan.putExtra(Intents.Scan.BEEP_ENABLED, false);
        scan.putExtra(Intents.Scan.ORIENTATION_LOCKED, false);
        scan.putExtra(Intents.Scan.SHOW_MISSING_CAMERA_PERMISSION_DIALOG, false);
        try {
            startActivityForResult(scan, PAIRING_SCAN_REQUEST);
        } catch (ActivityNotFoundException | SecurityException error) {
            toast("扫码组件启动失败");
        }
    }

    private void handlePairingIntent(Intent intent) {
        if (intent == null || catalogClient == null) return;
        Uri data = intent.getData();
        if (data == null || !"relayterm".equalsIgnoreCase(data.getScheme())
                || !"pair".equalsIgnoreCase(data.getHost())) return;
        intent.setData(null);
        handlePairingLink(data.toString());
    }

    private void handlePairingLink(String value) {
        PairingLink pairing;
        try {
            pairing = PairingLink.parse(value);
        } catch (IllegalArgumentException error) {
            toast("配对链接无效");
            return;
        }
        setStatus("正在配对…", SECONDARY);
        catalogClient.exchange(pairing.endpoint, pairing.challenge, new CatalogClient.PairingCallback() {
            @Override
            public void onPaired(CatalogClient.PairingResult result) {
                String connectionId = "pc-" + store.newId();
                TerminalProfile connection = new TerminalProfile(
                        connectionId, "RelayTerm PC", result.endpoint, result.token, "codex", "");
                store.upsert(connection);
                List<TerminalProfile> managed = new ArrayList<>();
                for (int i = 0; i < result.profiles.length(); i++) {
                    JSONObject item = result.profiles.optJSONObject(i);
                    if (item != null && item.optBoolean("enabled", true)) {
                        managed.add(TerminalProfile.managed(
                                connectionId, result.endpoint, result.token, item));
                    }
                }
                store.replaceManaged(connectionId, managed);
                String selected = managed.isEmpty() ? connectionId : managed.get(0).id;
                reloadProfiles(selected);
                setStatus("配对完成", ACCENT);
                syncCatalogs();
            }

            @Override
            public void onError(String message) {
                setStatus(message, DANGER);
            }
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode != PAIRING_SCAN_REQUEST) {
            super.onActivityResult(requestCode, resultCode, data);
            return;
        }
        if (resultCode != RESULT_OK) {
            boolean missingPermission = data != null
                    && data.getBooleanExtra(Intents.Scan.MISSING_CAMERA_PERMISSION, false);
            toast(missingPermission ? "请授予相机权限后重试" : "已取消扫码");
            return;
        }
        String value = data == null ? "" : data.getStringExtra(Intents.Scan.RESULT);
        handlePairingLink(value);
    }

    private EditText field(String hint, String value, boolean password) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setText(value);
        field.setTextColor(PRIMARY);
        field.setHintTextColor(SECONDARY);
        field.setTextSize(14);
        field.setSingleLine(true);
        field.setPadding(0, dp(7), 0, dp(7));
        if (password) field.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        field.setLayoutParams(new LinearLayout.LayoutParams(-1, dp(50)));
        return field;
    }

    private Button actionButton(String text, int icon) {
        Button button = new Button(this);
        button.setText(text);
        button.setTextSize(12);
        button.setTextColor(PRIMARY);
        button.setAllCaps(false);
        button.setMinHeight(0);
        button.setMinWidth(0);
        button.setPadding(dp(5), 0, dp(5), 0);
        if (icon != 0) {
            button.setCompoundDrawablePadding(dp(3));
            button.setCompoundDrawablesWithIntrinsicBounds(icon, 0, 0, 0);
        }
        button.setTextColor(new ColorStateList(
                new int[][]{{-android.R.attr.state_enabled}, {android.R.attr.state_pressed}, {}},
                new int[]{SECONDARY, PRIMARY, PRIMARY}));
        button.setBackground(buttonBackground(5));
        return button;
    }

    private StateListDrawable buttonBackground(int radius) {
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[]{-android.R.attr.state_enabled},
                panelBackground(SURFACE, OUTLINE, radius));
        states.addState(new int[]{android.R.attr.state_pressed},
                panelBackground(Color.rgb(31, 54, 59), ACCENT, radius));
        states.addState(new int[]{}, panelBackground(RAISED, OUTLINE, radius));
        return states;
    }

    private void styleDialog(AlertDialog dialog, boolean danger) {
        if (dialog == null) return;
        Window window = dialog.getWindow();
        if (window != null) window.setBackgroundDrawable(panelBackground(SURFACE, OUTLINE, 8));
        TextView message = dialog.findViewById(android.R.id.message);
        if (message != null) message.setTextColor(SECONDARY);
        Button positive = dialog.getButton(AlertDialog.BUTTON_POSITIVE);
        Button negative = dialog.getButton(AlertDialog.BUTTON_NEGATIVE);
        Button neutral = dialog.getButton(AlertDialog.BUTTON_NEUTRAL);
        if (positive != null) positive.setTextColor(danger ? DANGER : ACCENT);
        if (negative != null) negative.setTextColor(SECONDARY);
        if (neutral != null) neutral.setTextColor(WARNING);
    }

    private ImageButton iconButton(int icon, String description) {
        ImageButton button = new ImageButton(this);
        button.setImageResource(icon);
        button.setContentDescription(description);
        button.setTooltipText(description);
        button.setColorFilter(ACCENT);
        button.setBackground(buttonBackground(5));
        return button;
    }

    private LinearLayout row() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        return row;
    }

    private TextView label(String text, int size, int color) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(size);
        view.setTextColor(color);
        return view;
    }

    private GradientDrawable panelBackground(int color, int stroke, int radius) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radius));
        drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    private void setStatus(String value, int color) {
        statusView.setText(value);
        statusView.setTextColor(color);
    }

    private int findSelectedIndex(String id) {
        for (int i = 0; i < profiles.size(); i++) if (profiles.get(i).id.equals(id)) return i;
        return 0;
    }

    private int findIndexById(String id) {
        for (int i = 0; i < profiles.size(); i++) if (profiles.get(i).id.equals(id)) return i;
        return 0;
    }

    private boolean isSelected(String profileId) {
        return !profiles.isEmpty() && selectedIndex >= 0 && selectedIndex < profiles.size()
                && profiles.get(selectedIndex).id.equals(profileId);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }

    @Override
    protected void onResume() {
        super.onResume();
        foreground = true;
        syncCatalogs();
        if (!profiles.isEmpty()) {
            TerminalProfile selected = profiles.get(selectedIndex);
            if (!selected.isLocal() && requestedProfiles.contains(selected.id)
                    && !endedProfiles.contains(selected.id)
                    && (!ptyClient.isConnected() || !selected.id.equals(activePtyProfileId))) {
                main.post(() -> openPty(selected, true));
            }
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handlePairingIntent(intent);
    }

    @Override
    protected void onPause() {
        foreground = false;
        resetStopFlow();
        if (ptyClient.isConnected() || !activePtyProfileId.isEmpty()) ptyClient.disconnect();
        activePtyProfileId = "";
        super.onPause();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        if (!profiles.isEmpty()) {
            String id = profiles.get(selectedIndex).id;
            outState.putBoolean("resumePty", requestedProfiles.contains(id));
        }
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onDestroy() {
        resetStopFlow();
        ptyClient.shutdown();
        catalogClient.shutdown();
        localRunner.shutdown();
        super.onDestroy();
    }
}
