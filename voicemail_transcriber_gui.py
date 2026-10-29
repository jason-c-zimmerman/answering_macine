import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import threading
import queue
import subprocess
import sys
import os
import json
from datetime import datetime

from faster_whisper import WhisperModel


APP_DIR = Path.home() / ".simple_voicemail_transcriber"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = APP_DIR / "messages.json"


class VoicemailTranscriberApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Voicemail Transcriber")
        self.root.geometry("1050x700")
        self.root.minsize(860, 580)

        self.model_name = tk.StringVar(value="small")
        self.language = tk.StringVar(value="Auto")
        self.status = tk.StringVar(value="Ready")
        self.output_dir = tk.StringVar(value=str(Path.home() / "voicemail_transcripts"))

        self.worker_queue = queue.Queue()
        self.current_model = None
        self.current_model_name = None
        self.messages = self._load_messages()
        self.selected_message_id = None

        self._build_ui()
        self._refresh_message_list()
        self.root.after(100, self._check_queue)

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x")

        ttk.Label(
            header,
            text="Voicemail Transcriber",
            font=("TkDefaultFont", 18, "bold")
        ).pack(side="left")

        ttk.Button(
            header,
            text="Import WAV / Audio",
            command=self._choose_audio
        ).pack(side="right")

        settings = ttk.Frame(main)
        settings.pack(fill="x", pady=(10, 8))

        ttk.Label(settings, text="Model:").pack(side="left")
        ttk.Combobox(
            settings,
            textvariable=self.model_name,
            values=["tiny", "base", "small", "medium", "large-v3"],
            state="readonly",
            width=12
        ).pack(side="left", padx=(5, 15))

        ttk.Label(settings, text="Language:").pack(side="left")
        ttk.Combobox(
            settings,
            textvariable=self.language,
            values=["Auto", "English"],
            state="readonly",
            width=12
        ).pack(side="left", padx=(5, 15))

        ttk.Label(settings, text="Output folder:").pack(side="left")
        ttk.Entry(
            settings,
            textvariable=self.output_dir
        ).pack(side="left", fill="x", expand=True, padx=(5, 5))

        ttk.Button(
            settings,
            text="Browse",
            command=self._choose_output_dir
        ).pack(side="left")

        paned = ttk.Panedwindow(main, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=(4, 8))

        left = ttk.Frame(paned, padding=(0, 0, 6, 0))
        right = ttk.Frame(paned, padding=(6, 0, 0, 0))
        paned.add(left, weight=2)
        paned.add(right, weight=3)

        ttk.Label(
            left,
            text="Message Inbox",
            font=("TkDefaultFont", 12, "bold")
        ).pack(anchor="w", pady=(0, 5))

        columns = ("date", "file", "status")
        self.tree = ttk.Treeview(
            left,
            columns=columns,
            show="headings",
            selectmode="browse"
        )

        self.tree.heading("date", text="Date / Time")
        self.tree.heading("file", text="Recording")
        self.tree.heading("status", text="Status")

        self.tree.column("date", width=145, anchor="w")
        self.tree.column("file", width=220, anchor="w")
        self.tree.column("status", width=90, anchor="center")

        tree_scroll = ttk.Scrollbar(
            left,
            orient="vertical",
            command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=tree_scroll.set)

        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_message)

        ttk.Label(
            right,
            text="Message Details",
            font=("TkDefaultFont", 12, "bold")
        ).pack(anchor="w", pady=(0, 5))

        details = ttk.Frame(right)
        details.pack(fill="x")

        self.details_label = ttk.Label(
            details,
            text="Select a message.",
            justify="left"
        )
        self.details_label.pack(anchor="w")

        button_row = ttk.Frame(right)
        button_row.pack(fill="x", pady=(10, 8))

        self.play_button = ttk.Button(
            button_row,
            text="Play Recording",
            command=self._play_selected,
            state="disabled"
        )
        self.play_button.pack(side="left")

        self.open_button = ttk.Button(
            button_row,
            text="Open Folder",
            command=self._open_selected_folder,
            state="disabled"
        )
        self.open_button.pack(side="left", padx=(6, 0))

        self.delete_button = ttk.Button(
            button_row,
            text="Delete Entry",
            command=self._delete_selected,
            state="disabled"
        )
        self.delete_button.pack(side="left", padx=(6, 0))

        self.save_button = ttk.Button(
            button_row,
            text="Save Transcript As...",
            command=self._save_selected_as,
            state="disabled"
        )
        self.save_button.pack(side="left", padx=(6, 0))

        transcript_frame = ttk.LabelFrame(right, text="Transcript", padding=8)
        transcript_frame.pack(fill="both", expand=True)

        self.transcript = tk.Text(
            transcript_frame,
            wrap="word",
            font=("TkDefaultFont", 11)
        )
        self.transcript.pack(side="left", fill="both", expand=True)

        transcript_scroll = ttk.Scrollbar(
            transcript_frame,
            orient="vertical",
            command=self.transcript.yview
        )
        transcript_scroll.pack(side="right", fill="y")
        self.transcript.configure(yscrollcommand=transcript_scroll.set)

        footer = ttk.Frame(main)
        footer.pack(fill="x")

        self.progress = ttk.Progressbar(
            footer,
            mode="indeterminate",
            length=160
        )
        self.progress.pack(side="left")

        ttk.Label(
            footer,
            textvariable=self.status
        ).pack(side="left", padx=(10, 0))

    def _choose_audio(self):
        filename = filedialog.askopenfilename(
            title="Select voicemail recording",
            filetypes=[
                ("Audio files", "*.wav *.mp3 *.m4a *.flac *.ogg"),
                ("WAV files", "*.wav"),
                ("All files", "*.*"),
            ],
        )

        if not filename:
            return

        output_dir = Path(self.output_dir.get()).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)

        self.status.set("Preparing transcription...")
        self.progress.start(10)

        thread = threading.Thread(
            target=self._transcribe_worker,
            args=(Path(filename), output_dir),
            daemon=True
        )
        thread.start()

    def _choose_output_dir(self):
        folder = filedialog.askdirectory(title="Select transcript output folder")
        if folder:
            self.output_dir.set(folder)

    def _get_model(self):
        selected = self.model_name.get()

        if self.current_model is None or self.current_model_name != selected:
            self.worker_queue.put(
                ("status", f"Loading Whisper '{selected}' on CPU...")
            )
            self.current_model = WhisperModel(
                selected,
                device="cpu",
                compute_type="int8"
            )
            self.current_model_name = selected

        return self.current_model

    def _transcribe_worker(self, audio_path, output_dir):
        try:
            model = self._get_model()
            language = None if self.language.get() == "Auto" else "en"

            self.worker_queue.put(
                ("status", f"Transcribing {audio_path.name}...")
            )

            segments, info = model.transcribe(
                str(audio_path),
                beam_size=5,
                vad_filter=True,
                language=language
            )

            lines = []
            for segment in segments:
                text = segment.text.strip()
                if text:
                    lines.append(text)

            transcript = "\n".join(lines).strip()

            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            safe_stamp = now.strftime("%Y%m%d_%H%M%S")

            transcript_path = output_dir / f"{audio_path.stem}_{safe_stamp}.txt"
            transcript_path.write_text(transcript + "\n", encoding="utf-8")

            message = {
                "id": f"{safe_stamp}_{len(self.messages)+1}",
                "created": timestamp,
                "audio_path": str(audio_path.resolve()),
                "transcript_path": str(transcript_path.resolve()),
                "filename": audio_path.name,
                "transcript": transcript,
                "language": getattr(info, "language", "unknown"),
                "status": "Transcribed"
            }

            self.worker_queue.put(("done", message))

        except Exception as exc:
            self.worker_queue.put(("error", str(exc)))

    def _check_queue(self):
        try:
            while True:
                item = self.worker_queue.get_nowait()
                kind = item[0]

                if kind == "status":
                    self.status.set(item[1])

                elif kind == "done":
                    message = item[1]
                    self.messages.insert(0, message)
                    self._save_messages()
                    self._refresh_message_list()
                    self.progress.stop()
                    self.status.set(f"Finished: {message['filename']}")

                    if self.tree.get_children():
                        first = self.tree.get_children()[0]
                        self.tree.selection_set(first)
                        self.tree.focus(first)
                        self._on_select_message()

                elif kind == "error":
                    self.progress.stop()
                    self.status.set("Transcription failed")
                    messagebox.showerror("Transcription error", item[1])

        except queue.Empty:
            pass

        self.root.after(100, self._check_queue)

    def _refresh_message_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        for message in self.messages:
            self.tree.insert(
                "",
                "end",
                iid=message["id"],
                values=(
                    message["created"],
                    message["filename"],
                    message["status"]
                )
            )

    def _on_select_message(self, event=None):
        selected = self.tree.selection()

        if not selected:
            return

        message_id = selected[0]
        self.selected_message_id = message_id
        message = self._message_by_id(message_id)

        if not message:
            return

        self.details_label.config(
            text=(
                f"Received: {message['created']}\n"
                f"Recording: {message['filename']}\n"
                f"Language: {message.get('language', 'unknown')}"
            )
        )

        self.transcript.delete("1.0", "end")
        self.transcript.insert("1.0", message.get("transcript", ""))

        self.play_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.delete_button.configure(state="normal")
        self.save_button.configure(state="normal")

    def _message_by_id(self, message_id):
        for message in self.messages:
            if message["id"] == message_id:
                return message
        return None

    def _play_selected(self):
        message = self._message_by_id(self.selected_message_id)
        if not message:
            return

        path = Path(message["audio_path"])

        if not path.exists():
            messagebox.showerror(
                "Recording missing",
                f"The recording no longer exists:\n{path}"
            )
            return

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Playback error", str(exc))

    def _open_selected_folder(self):
        message = self._message_by_id(self.selected_message_id)
        if not message:
            return

        path = Path(message["transcript_path"]).parent

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Open folder error", str(exc))

    def _delete_selected(self):
        message = self._message_by_id(self.selected_message_id)
        if not message:
            return

        confirmed = messagebox.askyesno(
            "Delete entry",
            "Remove this message from the inbox?\n\n"
            "The original audio and transcript files will NOT be deleted."
        )

        if not confirmed:
            return

        self.messages = [
            m for m in self.messages
            if m["id"] != self.selected_message_id
        ]

        self.selected_message_id = None
        self._save_messages()
        self._refresh_message_list()

        self.details_label.config(text="Select a message.")
        self.transcript.delete("1.0", "end")

        for button in (
            self.play_button,
            self.open_button,
            self.delete_button,
            self.save_button
        ):
            button.configure(state="disabled")

        self.status.set("Message removed from inbox")

    def _save_selected_as(self):
        message = self._message_by_id(self.selected_message_id)
        if not message:
            return

        filename = filedialog.asksaveasfilename(
            title="Save transcript",
            defaultextension=".txt",
            initialfile=Path(message["filename"]).stem + ".txt",
            filetypes=[
                ("Text files", "*.txt"),
                ("All files", "*.*")
            ]
        )

        if filename:
            Path(filename).write_text(
                message.get("transcript", "") + "\n",
                encoding="utf-8"
            )
            self.status.set(f"Saved: {filename}")

    def _load_messages(self):
        if not DB_FILE.exists():
            return []

        try:
            return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_messages(self):
        DB_FILE.write_text(
            json.dumps(self.messages, indent=2),
            encoding="utf-8"
        )


def main():
    root = tk.Tk()
    VoicemailTranscriberApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
