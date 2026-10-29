import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import subprocess
import sys
import os
import json

APP_DIR = Path.home() / ".simple_voicemail_transcriber"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = APP_DIR / "messages.json"


class VoicemailInbox:
    def __init__(self, root):
        self.root = root
        self.root.title("Voicemail Inbox")
        self.root.geometry("1100x700")
        self.root.minsize(850, 550)

        self.messages = []
        self.selected_message_id = None
        self.last_db_mtime = None
        self.status = tk.StringVar(value="Ready")

        self._build_ui()
        self._reload_messages(force=True)
        self.root.after(1000, self._poll_database)

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x")

        ttk.Label(
            header,
            text="Voicemail Inbox",
            font=("TkDefaultFont", 18, "bold")
        ).pack(side="left")

        ttk.Button(
            header,
            text="Refresh",
            command=lambda: self._reload_messages(force=True)
        ).pack(side="right")

        pane = ttk.Panedwindow(main, orient="horizontal")
        pane.pack(fill="both", expand=True, pady=(12, 8))

        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=2)
        pane.add(right, weight=3)

        columns = ("date", "caller", "status")
        self.tree = ttk.Treeview(
            left,
            columns=columns,
            show="headings",
            selectmode="browse"
        )

        self.tree.heading("date", text="Date / Time")
        self.tree.heading("caller", text="Caller")
        self.tree.heading("status", text="Status")

        self.tree.column("date", width=155)
        self.tree.column("caller", width=170)
        self.tree.column("status", width=100, anchor="center")

        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        ttk.Label(
            right,
            text="Message Details",
            font=("TkDefaultFont", 12, "bold")
        ).pack(anchor="w")

        self.details = ttk.Label(right, text="Select a voicemail.", justify="left")
        self.details.pack(anchor="w", pady=(6, 10))

        buttons = ttk.Frame(right)
        buttons.pack(fill="x", pady=(0, 8))

        self.play_button = ttk.Button(
            buttons, text="Play Recording", command=self._play, state="disabled"
        )
        self.play_button.pack(side="left")

        self.folder_button = ttk.Button(
            buttons, text="Open Folder", command=self._open_folder, state="disabled"
        )
        self.folder_button.pack(side="left", padx=(6, 0))

        self.save_button = ttk.Button(
            buttons, text="Save Transcript As...", command=self._save_as, state="disabled"
        )
        self.save_button.pack(side="left", padx=(6, 0))

        self.delete_button = ttk.Button(
            buttons, text="Delete Entry", command=self._delete, state="disabled"
        )
        self.delete_button.pack(side="left", padx=(6, 0))

        frame = ttk.LabelFrame(right, text="Transcript", padding=8)
        frame.pack(fill="both", expand=True)

        self.text = tk.Text(frame, wrap="word", font=("TkDefaultFont", 11))
        self.text.pack(side="left", fill="both", expand=True)

        text_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        text_scroll.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=text_scroll.set)

        ttk.Label(main, textvariable=self.status).pack(anchor="w")

    def _poll_database(self):
        self._reload_messages()
        self.root.after(1000, self._poll_database)

    def _reload_messages(self, force=False):
        try:
            mtime = DB_FILE.stat().st_mtime if DB_FILE.exists() else None
        except OSError:
            mtime = None

        if not force and mtime == self.last_db_mtime:
            return

        self.last_db_mtime = mtime

        if not DB_FILE.exists():
            self.messages = []
        else:
            try:
                self.messages = json.loads(DB_FILE.read_text(encoding="utf-8"))
            except Exception as exc:
                self.status.set(f"Could not read inbox: {exc}")
                return

        previous_selection = self.selected_message_id

        for item in self.tree.get_children():
            self.tree.delete(item)

        for message in self.messages:
            self.tree.insert(
                "",
                "end",
                iid=message["id"],
                values=(
                    message.get("created", ""),
                    message.get("caller", "Unknown caller"),
                    message.get("status", "")
                )
            )

        if previous_selection and self._message(previous_selection):
            self.tree.selection_set(previous_selection)
            self.tree.focus(previous_selection)
        elif self.messages:
            first_id = self.messages[0]["id"]
            self.tree.selection_set(first_id)
            self.tree.focus(first_id)
            self.selected_message_id = first_id
            self._show_message(self.messages[0])

        self.status.set(f"{len(self.messages)} message(s)")

    def _on_select(self, event=None):
        selected = self.tree.selection()
        if not selected:
            return

        self.selected_message_id = selected[0]
        message = self._message(self.selected_message_id)
        if message:
            self._show_message(message)

    def _show_message(self, message):
        self.details.config(
            text=(
                f"Caller: {message.get('caller', 'Unknown caller')}\n"
                f"Received: {message.get('created', '')}\n"
                f"Recording: {message.get('filename', '')}\n"
                f"Language: {message.get('language', 'unknown')}"
            )
        )

        self.text.delete("1.0", "end")
        self.text.insert("1.0", message.get("transcript", ""))

        for button in (
            self.play_button,
            self.folder_button,
            self.save_button,
            self.delete_button,
        ):
            button.configure(state="normal")

    def _message(self, message_id):
        for message in self.messages:
            if message.get("id") == message_id:
                return message
        return None

    def _play(self):
        message = self._message(self.selected_message_id)
        if not message:
            return

        path = Path(message.get("audio_path", ""))
        if not path.exists():
            messagebox.showerror("Recording missing", str(path))
            return

        self._open_path(path)

    def _open_folder(self):
        message = self._message(self.selected_message_id)
        if not message:
            return

        path = Path(message.get("audio_path", "")).parent
        self._open_path(path)

    def _save_as(self):
        message = self._message(self.selected_message_id)
        if not message:
            return

        filename = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=Path(message.get("filename", "voicemail.wav")).stem + ".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )

        if filename:
            Path(filename).write_text(
                message.get("transcript", "") + "\n",
                encoding="utf-8"
            )

    def _delete(self):
        message = self._message(self.selected_message_id)
        if not message:
            return

        if not messagebox.askyesno(
            "Delete entry",
            "Remove this voicemail from the inbox?\n\n"
            "The WAV and TXT files will remain on disk."
        ):
            return

        self.messages = [
            m for m in self.messages
            if m.get("id") != self.selected_message_id
        ]

        DB_FILE.write_text(
            json.dumps(self.messages, indent=2),
            encoding="utf-8"
        )

        self.selected_message_id = None
        self.last_db_mtime = None
        self.details.config(text="Select a voicemail.")
        self.text.delete("1.0", "end")

        for button in (
            self.play_button,
            self.folder_button,
            self.save_button,
            self.delete_button,
        ):
            button.configure(state="disabled")

        self._reload_messages(force=True)

    def _open_path(self, path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Open error", str(exc))


def main():
    root = tk.Tk()
    VoicemailInbox(root)
    root.mainloop()


if __name__ == "__main__":
    main()
