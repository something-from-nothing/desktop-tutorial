package uk.deskagent.mobile;

import android.Manifest;
import android.content.ContentUris;
import android.content.Context;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.CalendarContract;
import android.provider.ContactsContract;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Reads the phone's own calendar and contacts for the AI, only after the user has
 * allowed it (Android asks them the first time). Nothing is written or sent anywhere.
 */
final class PhoneData {
    private PhoneData() {
    }

    static boolean allowed(Context context, String permission) {
        return context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED;
    }

    /** Events from now until `days` ahead, one per line, soonest first. */
    static String calendar(Context context, int days) {
        long now = System.currentTimeMillis();
        Uri.Builder range = CalendarContract.Instances.CONTENT_URI.buildUpon();
        ContentUris.appendId(range, now - 3_600_000L);
        ContentUris.appendId(range, now + Math.max(1, Math.min(days, 60)) * 86_400_000L);
        String[] cols = {CalendarContract.Instances.TITLE, CalendarContract.Instances.BEGIN, CalendarContract.Instances.END,
                CalendarContract.Instances.ALL_DAY, CalendarContract.Instances.EVENT_LOCATION};
        SimpleDateFormat day = new SimpleDateFormat("EEE d MMM", Locale.UK), time = new SimpleDateFormat("HH:mm", Locale.UK);
        StringBuilder out = new StringBuilder();
        int count = 0;
        try (Cursor c = context.getContentResolver().query(range.build(), cols, null, null, CalendarContract.Instances.BEGIN + " ASC")) {
            while (c != null && c.moveToNext() && count < 60) {
                Date begin = new Date(c.getLong(1)), end = new Date(c.getLong(2));
                boolean allDay = c.getInt(3) == 1;
                out.append(day.format(begin)).append(allDay ? ", all day" : ", " + time.format(begin) + " to " + time.format(end))
                        .append(": ").append(c.getString(0) == null ? "(no title)" : c.getString(0));
                if (c.getString(4) != null && !c.getString(4).isEmpty()) out.append(" (at ").append(c.getString(4)).append(")");
                out.append('\n');
                count++;
            }
        }
        return count == 0 ? "Nothing in the calendar for the next " + days + " days." : out.toString().trim();
    }

    /** Contacts whose name contains `name`, with their numbers and email addresses. */
    static String contacts(Context context, String name) {
        StringBuilder out = new StringBuilder();
        String like = "%" + name.replace("%", "") + "%";
        try (Cursor c = context.getContentResolver().query(ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                new String[]{ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER},
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " LIKE ?", new String[]{like},
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " ASC")) {
            while (c != null && c.moveToNext() && out.length() < 4000) {
                out.append(c.getString(0)).append(": phone ").append(c.getString(1)).append('\n');
            }
        }
        try (Cursor c = context.getContentResolver().query(ContactsContract.CommonDataKinds.Email.CONTENT_URI,
                new String[]{ContactsContract.CommonDataKinds.Email.DISPLAY_NAME_PRIMARY, ContactsContract.CommonDataKinds.Email.ADDRESS},
                ContactsContract.CommonDataKinds.Email.DISPLAY_NAME_PRIMARY + " LIKE ?", new String[]{like}, null)) {
            while (c != null && c.moveToNext() && out.length() < 6000) {
                out.append(c.getString(0)).append(": email ").append(c.getString(1)).append('\n');
            }
        }
        return out.length() == 0 ? "No contact matching \"" + name + "\"." : out.toString().trim();
    }

    static final String CALENDAR = Manifest.permission.READ_CALENDAR;
    static final String CONTACTS = Manifest.permission.READ_CONTACTS;
}
