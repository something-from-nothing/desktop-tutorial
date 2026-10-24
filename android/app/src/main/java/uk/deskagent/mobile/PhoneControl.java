package uk.deskagent.mobile;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Bundle;
import android.provider.Settings;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.util.ArrayList;
import java.util.List;

/**
 * Lets the AI use other apps, the same way screen readers do: it reads what is on
 * screen as a numbered list of buttons, fields and text, and can tap, type, scroll,
 * go back or open an app. It only acts when a chat asks it to, and only after you
 * have switched it on in Android's Accessibility settings.
 */
public class PhoneControl extends AccessibilityService {
    private static volatile PhoneControl running;
    private final List<AccessibilityNodeInfo> numbered = new ArrayList<>();

    @Override
    protected void onServiceConnected() {
        running = this;
    }

    @Override
    public boolean onUnbind(Intent intent) {
        running = null;
        return super.onUnbind(intent);
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
    }

    @Override
    public void onInterrupt() {
    }

    static boolean enabled() {
        return running != null;
    }

    static void openSettings(Context context) {
        context.startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }

    private static PhoneControl need() throws Exception {
        PhoneControl s = running;
        if (s == null) throw new Exception("Phone control is switched off. Turn on Desk Agent in Settings, Accessibility.");
        return s;
    }

    // ------------------------------------------------ reading the screen

    /** The screen as numbered lines, for example: [3] button "Send". */
    static synchronized String readScreen() throws Exception {
        PhoneControl s = need();
        AccessibilityNodeInfo root = s.getRootInActiveWindow();
        if (root == null) return "The screen can't be read right now (it may be locked or showing something private).";
        s.numbered.clear();
        StringBuilder out = new StringBuilder();
        CharSequence pkg = root.getPackageName();
        out.append("App on screen: ").append(appLabel(s, pkg == null ? "" : pkg.toString())).append("\n");
        walk(s, root, out, 0);
        if (s.numbered.isEmpty()) out.append("(nothing readable on screen)\n");
        return out.toString();
    }

    private static void walk(PhoneControl s, AccessibilityNodeInfo n, StringBuilder out, int depth) {
        if (n == null || !n.isVisibleToUser() || s.numbered.size() >= 150 || depth > 40) return;
        String text = n.getText() == null ? "" : n.getText().toString().trim();
        String desc = n.getContentDescription() == null ? "" : n.getContentDescription().toString().trim();
        String hint = n.getHintText() == null ? "" : n.getHintText().toString().trim();
        String label = !text.isEmpty() ? text : !desc.isEmpty() ? desc : hint;
        boolean useful = n.isClickable() || n.isEditable() || n.isCheckable() || (n.isScrollable() && depth < 12) || !label.isEmpty();
        if (useful) {
            s.numbered.add(n);
            out.append("[").append(s.numbered.size()).append("] ").append(kind(n));
            if (!label.isEmpty()) out.append(" \"").append(label.length() > 120 ? label.substring(0, 120) + "..." : label).append("\"");
            if (n.isCheckable()) out.append(n.isChecked() ? " (on)" : " (off)");
            if (n.isEditable() && !hint.isEmpty() && !hint.equals(label)) out.append(" hint \"").append(hint).append("\"");
            out.append("\n");
        }
        for (int i = 0; i < n.getChildCount(); i++) walk(s, n.getChild(i), out, depth + 1);
    }

    private static String kind(AccessibilityNodeInfo n) {
        if (n.isEditable()) return "text field";
        if (n.isCheckable()) return "switch";
        if (n.isClickable()) return "button";
        if (n.isScrollable()) return "scrolling list";
        return "text";
    }

    private static AccessibilityNodeInfo item(PhoneControl s, int number) throws Exception {
        if (number < 1 || number > s.numbered.size()) throw new Exception("There is no item " + number + ". Read the screen again first.");
        return s.numbered.get(number - 1);
    }

    // ------------------------------------------------ acting

    static synchronized String tap(int number) throws Exception {
        PhoneControl s = need();
        AccessibilityNodeInfo n = item(s, number);
        AccessibilityNodeInfo target = n;
        while (target != null && !target.isClickable()) target = target.getParent();
        if (target == null || !target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            Rect r = new Rect();
            n.getBoundsInScreen(r);
            gesture(s, r.centerX(), r.centerY(), r.centerX(), r.centerY(), 60);
        }
        settle();
        return "Tapped item " + number + ".";
    }

    static synchronized String type(int number, String text) throws Exception {
        PhoneControl s = need();
        AccessibilityNodeInfo field;
        if (number > 0) {
            field = item(s, number);
        } else {
            AccessibilityNodeInfo root = s.getRootInActiveWindow();
            field = root == null ? null : root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        }
        if (field == null || !field.isEditable()) throw new Exception("That isn't a text field. Read the screen and pick a text field's number.");
        field.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) throw new Exception("The app didn't accept the text.");
        settle();
        return "Typed into the field.";
    }

    static synchronized String scroll(String direction, int number) throws Exception {
        PhoneControl s = need();
        boolean down = !"up".equals(direction);
        AccessibilityNodeInfo list = number > 0 ? item(s, number) : null;
        while (list != null && !list.isScrollable()) list = list.getParent();
        if (list == null) list = firstScrollable(s.getRootInActiveWindow());
        boolean ok = list != null && list.performAction(down ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);
        if (!ok) {
            int w = s.getResources().getDisplayMetrics().widthPixels, h = s.getResources().getDisplayMetrics().heightPixels;
            gesture(s, w / 2, down ? h * 3 / 4 : h / 4, w / 2, down ? h / 4 : h * 3 / 4, 350);
        }
        settle();
        return "Scrolled " + (down ? "down" : "up") + ".";
    }

    private static AccessibilityNodeInfo firstScrollable(AccessibilityNodeInfo n) {
        if (n == null) return null;
        if (n.isScrollable() && n.isVisibleToUser()) return n;
        for (int i = 0; i < n.getChildCount(); i++) {
            AccessibilityNodeInfo f = firstScrollable(n.getChild(i));
            if (f != null) return f;
        }
        return null;
    }

    static String press(String button) throws Exception {
        PhoneControl s = need();
        int action;
        switch (button) {
            case "back": action = GLOBAL_ACTION_BACK; break;
            case "home": action = GLOBAL_ACTION_HOME; break;
            case "recents": action = GLOBAL_ACTION_RECENTS; break;
            case "notifications": action = GLOBAL_ACTION_NOTIFICATIONS; break;
            default: throw new Exception("Unknown button " + button);
        }
        s.performGlobalAction(action);
        settle();
        return "Pressed " + button + ".";
    }

    /** Opens the installed app whose name best matches. */
    static String openApp(Context context, String name) throws Exception {
        PackageManager pm = context.getPackageManager();
        Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        String want = name.trim().toLowerCase();
        ResolveInfo best = null;
        int bestScore = 0;
        for (ResolveInfo ri : pm.queryIntentActivities(launcher, 0)) {
            String label = ri.loadLabel(pm).toString().toLowerCase();
            int score = label.equals(want) ? 3 : label.startsWith(want) ? 2 : label.contains(want) ? 1 : 0;
            if (score > bestScore) { best = ri; bestScore = score; }
        }
        if (best == null) throw new Exception("No app called " + name + " is installed.");
        Intent open = pm.getLaunchIntentForPackage(best.activityInfo.packageName);
        if (open == null) throw new Exception("That app can't be opened directly.");
        context.startActivity(open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        Thread.sleep(1500);
        return "Opened " + best.loadLabel(pm) + ".";
    }

    static String listApps(Context context) {
        PackageManager pm = context.getPackageManager();
        Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<String> names = new ArrayList<>();
        for (ResolveInfo ri : pm.queryIntentActivities(launcher, 0)) names.add(ri.loadLabel(pm).toString());
        java.util.Collections.sort(names, String.CASE_INSENSITIVE_ORDER);
        return String.join(", ", names);
    }

    private static String appLabel(Context c, String pkg) {
        try {
            return c.getPackageManager().getApplicationLabel(c.getPackageManager().getApplicationInfo(pkg, 0)).toString();
        } catch (Exception e) {
            return pkg;
        }
    }

    private static void gesture(PhoneControl s, int x1, int y1, int x2, int y2, long ms) {
        Path p = new Path();
        p.moveTo(x1, y1);
        p.lineTo(x2, y2);
        s.dispatchGesture(new GestureDescription.Builder().addStroke(new GestureDescription.StrokeDescription(p, 0, ms)).build(), null, null);
    }

    private static void settle() {
        try {
            Thread.sleep(900);
        } catch (InterruptedException ignored) {
        }
    }
}
