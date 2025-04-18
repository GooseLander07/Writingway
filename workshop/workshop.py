import datetime
import tempfile
import pyaudio
import wave
import whisper
import json
import os
import re
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit,
    QPushButton, QMessageBox, QInputDialog,
    QSplitter, QWidget, QLabel, QApplication, QListWidget, QListWidgetItem, QMenu, QComboBox
)
from PyQt5.QtCore import Qt, QPoint, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon, QCursor, QPixmap  # Added QCursor and QPixmap for cursor manipulation
import muse.prompt_utils
from settings.llm_api_aggregator import WWApiAggregator
from settings.autosave_manager import load_latest_autosave
from .conversation_history_manager import estimate_conversation_tokens, summarize_conversation
from .embedding_manager import EmbeddingIndex
from compendium.context_panel import ContextPanel

TOKEN_LIMIT = 2000

class WorkshopWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workshop")
        # Allow the user to maximize the window by including the maximize hint.
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.resize(1000, 600)
        # Optionally, start maximized:
        self.setWindowState(self.windowState() | Qt.WindowMaximized)
        self.model = getattr(parent, "model", None) if parent else None  # Access model from parent if available
        self.project_name = getattr(self.model, "project_name", "DefaultProject") if parent else "DefaultProject"
        self.structure = getattr(self.model, "structure", {"acts": []}) if parent else {"acts": []}
        self.workshop_prompt_config = None

        # Conversation management
        self.conversation_history = []   # current active conversation history
        self.conversations = {}          # dict mapping conversation name -> history list
        self.current_conversation = "Chat 1"
        self.conversations[self.current_conversation] = []

        # Initialize the embedding index for context retrieval.
        self.embedding_index = EmbeddingIndex()

        self.current_mode = "Normal"
        
        # Audio recording variables
        self.pause_start = None  # Track when pause begins
        self.available_models = self.get_available_models()  # Get installed models

        # Define custom cursors for transcription
        self.waiting_cursor = QCursor(QPixmap("assets/icons/clock.svg"))  # Waiting cursor with clock icon
        self.normal_cursor = QCursor()  # Default system cursor

        self.init_ui()
        self.load_conversations()

        # Connect model signal if available
        if self.model:
            self.model.structureChanged.connect(self.context_panel.on_structure_changed)

    def get_available_models(self):
        """Retrieves a list of installed models from cache"""
        cache_dir = os.path.expanduser("~/.cache/whisper")
        models = []
        if os.path.exists(cache_dir):
            for file in os.listdir(cache_dir):
                if file.endswith(".pt"):
                    model_name = file.split(".")[0]
                    models.append(model_name)
        return models or ["tiny"]  # Default to tiny if no models found

    def init_ui(self):
        main_layout = QVBoxLayout(self)

        # Outer splitter divides conversation list and the chat panel.
        outer_splitter = QSplitter(Qt.Horizontal)

        # Conversation History Panel
        conversation_container = QWidget()
        conversation_layout = QVBoxLayout(conversation_container)
        conversation_layout.setContentsMargins(0, 0, 0, 0)

        self.conversation_list = QListWidget()
        self.conversation_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.conversation_list.customContextMenuRequested.connect(self.show_conversation_context_menu)
        self.conversation_list.itemClicked.connect(self.load_conversation_from_list)
        conversation_layout.addWidget(self.conversation_list)

        new_chat_button = QPushButton("New Chat")
        new_chat_button.clicked.connect(self.new_conversation)
        conversation_layout.addWidget(new_chat_button)

        outer_splitter.addWidget(conversation_container)
        outer_splitter.setStretchFactor(0, 1)

        # Chat Panel
        chat_panel = QWidget()
        chat_layout = QVBoxLayout(chat_panel)

        # Chat log (display area)
        self.chat_log = QTextEdit()
        self.chat_log.setReadOnly(True)
        chat_layout.addWidget(self.chat_log)

        # Splitter for input and context panel
        splitter = QSplitter(Qt.Horizontal)

        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        self.chat_input = QTextEdit()
        self.chat_input.setPlaceholderText("Type your message here...")
        left_layout.addWidget(self.chat_input)

        # Buttons and Mode/Prompt Selector
        buttons_layout = QHBoxLayout()

        # Add pulldown menu for mode selection (Normal, Economy, Ultra-Light)
        self.mode_selector = QComboBox()
        self.mode_selector.addItems(["Normal", "Economy", "Ultra-Light"])
        self.mode_selector.currentIndexChanged.connect(self.mode_changed)
        buttons_layout.addWidget(self.mode_selector)

        # Workshop Prompt pulldown menu instead of a button.
        self.prompt_selector = QComboBox()
        prompts = muse.prompt_utils.get_workshop_prompts(self.project_name)
        if prompts:
            for p in prompts:
                name = p.get("name", "Unnamed")
                # Store the entire prompt configuration as the associated data.
                self.prompt_selector.addItem(name, p)
        else:
            self.prompt_selector.addItem("No Workshop Prompts")
            self.prompt_selector.setEnabled(False)
        self.prompt_selector.currentIndexChanged.connect(self.prompt_selection_changed)
        buttons_layout.addWidget(self.prompt_selector)

        # Send button: remove text and set icon from assets/icons folder
        self.send_button = QPushButton()
        self.send_button.setIcon(QIcon("assets/icons/send.svg"))
        self.send_button.clicked.connect(self.send_message)
        buttons_layout.addWidget(self.send_button)

        # Context button: remove text and set icon; update icon on toggle
        self.context_button = QPushButton()
        self.context_button.setCheckable(True)
        self.context_button.setIcon(QIcon("assets/icons/book.svg"))
        self.context_button.clicked.connect(self.toggle_context_panel)
        buttons_layout.addWidget(self.context_button)
        
        # Audio recording and transcription section
        audio_group_layout = QHBoxLayout()
        
        # Record button
        self.record_button = QPushButton()
        self.record_button.setIcon(QIcon("assets/icons/mic.svg"))
        self.record_button.setCheckable(True)
        self.record_button.clicked.connect(self.toggle_recording)
        audio_group_layout.addWidget(self.record_button)
        
        # Pause button
        self.pause_button = QPushButton()
        self.pause_button.setIcon(QIcon("assets/icons/pause.svg"))
        self.pause_button.setCheckable(True)
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        audio_group_layout.addWidget(self.pause_button)
        
        # Recording time display
        self.time_label = QLabel("00:00")
        audio_group_layout.addWidget(self.time_label)
        
        # Whisper model selection - only show installed models
        audio_group_layout.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.addItems(self.available_models)
        audio_group_layout.addWidget(self.model_combo)
        
        # Language selection with expanded language options
        audio_group_layout.addWidget(QLabel("Language:"))
        self.language_combo = QComboBox()
        self.language_combo.addItems([
            "Auto", "English", "Polish", "Spanish", "French", "German", 
            "Italian", "Portuguese", "Russian", "Japanese", "Chinese", 
            "Korean", "Dutch", "Arabic", "Hindi", "Swedish", "Czech", 
            "Finnish", "Turkish", "Greek", "Ukrainian"
        ])
        audio_group_layout.addWidget(self.language_combo)
        
        # Add audio group to the main buttons layout
        buttons_layout.addLayout(audio_group_layout)
        
        # Timer for updating recording time
        self.recording_timer = QTimer()
        self.recording_timer.timeout.connect(self.update_recording_time)

        # Model label
        self.model_label = QLabel("Model: [None]")
        buttons_layout.addWidget(self.model_label)
        buttons_layout.addStretch()

        left_layout.addLayout(buttons_layout)
        splitter.addWidget(left_container)

        # Context Panel
        self.context_panel = ContextPanel(self.structure, self.project_name, parent=self)
        self.context_panel.setVisible(False)
        splitter.addWidget(self.context_panel)
        splitter.setSizes([500, 300])

        chat_layout.addWidget(splitter)
        outer_splitter.addWidget(chat_panel)
        outer_splitter.setStretchFactor(1, 3)

        main_layout.addWidget(outer_splitter)

    def prompt_selection_changed(self, index):
        """Called when a workshop prompt is selected from the pulldown menu."""
        if not self.prompt_selector.isEnabled():
            return
        prompt_config = self.prompt_selector.itemData(index)
        if prompt_config:
            self.workshop_prompt_config = prompt_config
            provider = prompt_config.get("provider", "Local")
            model = prompt_config.get("model", "Local Model")
            self.model_label.setText(f"Model: {provider}/{model}")

    def mode_changed(self, index):
        mode = self.mode_selector.currentText()
        print("Selected mode:", mode)
        self.current_mode = mode

    def toggle_context_panel(self):
        if self.context_panel.isVisible():
            self.context_panel.setVisible(False)
            self.context_button.setIcon(QIcon("assets/icons/book.svg"))
        else:
            self.context_panel.setVisible(True)
            self.context_button.setIcon(QIcon("assets/icons/book-open.svg"))

    def new_conversation(self):
        new_chat_number = self.conversation_list.count() + 1
        new_chat_name = f"Chat {new_chat_number}"
        self.conversations[new_chat_name] = []
        self.conversation_list.addItem(new_chat_name)
        self.current_conversation = new_chat_name
        self.conversation_history = self.conversations[new_chat_name]
        self.chat_log.clear()
        self.save_conversations()

    def get_scene_text(self, scene_name):
        print(f"DEBUG: Looking for scene content for: {scene_name}")
        for act in self.structure.get("acts", []):
            for chapter in act.get("chapters", []):
                for scene in chapter.get("scenes", []):
                    if scene.get("name", "").lower() == scene_name.lower():
                        hierarchy =  [act.get("name"), chapter.get("name"), scene.get("name")]
                        content = load_latest_autosave(self.project_name, hierarchy, scene)
                        return content or f"[No content for scene {scene_name}]"
        print(f"DEBUG: No scene content found for: {scene_name}")
        return f"[No content for scene {scene_name}]"

    def get_item_hierarchy(self, item):
        """Helper method to get the hierarchy of a tree item (used by ContextPanel)."""
        hierarchy = []
        while item:
            hierarchy.insert(0, item.text(0))
            item = item.parent()
        return hierarchy

    def send_message(self):
        user_message = self.chat_input.toPlainText().strip()
        if not user_message:
            return

        augmented_message = user_message

        # Add selected context from ContextPanel
        context_text = self.context_panel.get_selected_context_text()
        if context_text:
            augmented_message += "\n\nContext:\n" + context_text

        # Append message to chat log.
        self.chat_log.append("You: " + user_message)
        self.chat_input.clear()
        self.chat_log.append("LLM: Generating response...")
        QApplication.processEvents()

        # Maintain conversation history.
        conversation_payload = list(self.conversation_history)
        if not conversation_payload and self.workshop_prompt_config:
            conversation_payload.append({
                "role": "system",
                "content": self.workshop_prompt_config.get("text", "")
            })
        conversation_payload.append({"role": "user", "content": augmented_message})

        # Depending on the mode, adjust overrides or summarization strategies.
        overrides = {}
        if self.workshop_prompt_config:
            overrides = {
                "provider": self.workshop_prompt_config.get("provider", "Local"),
                "model": self.workshop_prompt_config.get("model", "Local Model"),
                "max_tokens": self.workshop_prompt_config.get("max_tokens", 2000),
                "temperature": self.workshop_prompt_config.get("temperature", 1.0)
            }

        try:
            # Summarize or prune conversation if token limit is exceeded.
            if estimate_conversation_tokens(conversation_payload) > TOKEN_LIMIT:
                summary = summarize_conversation(conversation_payload)
                conversation_payload = [conversation_payload[0], {"role": "system", "content": summary}]
                self.chat_log.append("LLM: [Conversation summarized to reduce token count.]")

            # Retrieve additional context using FAISS based on the user message.
            retrieved_context = self.embedding_index.query(user_message)
            if retrieved_context:
                augmented_message += "\n[Retrieved Context]:\n" + "\n".join(retrieved_context)

            token_count = estimate_conversation_tokens(conversation_payload)
            self.model_label.setText(
                f"Model: {overrides.get('provider', 'Local')}/{overrides.get('model', 'Local Model')} ({token_count} tokens sent)"
            )

            response = WWApiAggregator.send_prompt_to_llm("", overrides=overrides, conversation_history=conversation_payload)

            # Format the response to render Markdown-style bold and italic text as HTML.
            formatted_response = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", response)  # Bold
            formatted_response = re.sub(r"\*(.*?)\*", r"<i>\1</i>", formatted_response)  # Italic

            # Replace newlines with <br> to preserve paragraph breaks in HTML.
            formatted_response = formatted_response.replace("\n", "<br>")

            # Append the formatted response to the chat log using insertHtml to preserve structure.
            self.chat_log.insertHtml(f"<p><b>LLM:</b> {formatted_response}</p>")
            self.chat_log.insertHtml("<br>")  # Add a blank line for spacing.
            QApplication.processEvents()

            self.conversation_history = conversation_payload
            self.conversation_history.append({"role": "assistant", "content": response})
            self.conversations[self.current_conversation] = self.conversation_history
            self.save_conversations()

            # Add the user message to the embedding index for future context retrieval.
            self.embedding_index.add_text(user_message)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to generate response: {e}")

    def load_conversation_from_list(self, item: QListWidgetItem):
        selected_name = item.text()
        self.current_conversation = selected_name
        self.conversation_history = self.conversations.get(selected_name, [])
        self.chat_log.clear()
        for msg in self.conversation_history:
            role = msg.get("role", "Unknown")
            content = msg.get("content", "")

            # Format Markdown-style bold and italic text as HTML.
            formatted_content = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", content)  # Bold
            formatted_content = re.sub(r"\*(.*?)\*", r"<i>\1</i>", formatted_content)  # Italic

            # Replace newlines with <br> to preserve paragraph breaks in HTML.
            formatted_content = formatted_content.replace("\n", "<br>")

            # Append the formatted content to the chat log.
            self.chat_log.insertHtml(f"<p><b>{role.capitalize()}:</b> {formatted_content}</p>")
            self.chat_log.insertHtml("<br>")  # Add a blank line for spacing.

    def show_conversation_context_menu(self, pos: QPoint):
        item = self.conversation_list.itemAt(pos)
        if item is None:
            return
        menu = QMenu()
        rename_action = menu.addAction("Rename")
        delete_action = menu.addAction("Delete")
        action = menu.exec_(self.conversation_list.mapToGlobal(pos))
        if action == rename_action:
            self.rename_conversation(item)
        elif action == delete_action:
            self.delete_conversation(item)

    def rename_conversation(self, item: QListWidgetItem):
        current_name = item.text()
        new_name, ok = QInputDialog.getText(self, "Rename Conversation", "Enter new conversation name:", text=current_name)
        if ok and new_name.strip():
            new_name = new_name.strip()
            self.conversations[new_name] = self.conversations.pop(current_name)
            item.setText(new_name)
            if self.current_conversation == current_name:
                self.current_conversation = new_name
            self.save_conversations()

    def delete_conversation(self, item: QListWidgetItem):
        conversation_name = item.text()
        reply = QMessageBox.question(self, "Delete Conversation", f"Are you sure you want to delete '{conversation_name}'?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            row = self.conversation_list.row(item)
            self.conversation_list.takeItem(row)
            if conversation_name in self.conversations:
                del self.conversations[conversation_name]
            if self.current_conversation == conversation_name:
                if self.conversation_list.count() > 0:
                    new_item = self.conversation_list.item(0)
                    self.current_conversation = new_item.text()
                    self.conversation_history = self.conversations.get(self.current_conversation, [])
                    self.load_conversation_from_list(new_item)
                else:
                    self.current_conversation = "Chat 1"
                    self.conversations[self.current_conversation] = []
                    self.conversation_list.addItem(self.current_conversation)
                    self.chat_log.clear()
            self.save_conversations()

    def load_conversations(self):
        """Load saved conversations from a JSON file (if it exists) and update the conversation list widget."""
        if os.path.exists("conversations.json"):
            try:
                with open("conversations.json", "r", encoding="utf-8") as f:
                    self.conversations = json.load(f)
                # Update the conversation list widget
                self.conversation_list.clear()
                for conv_name in self.conversations:
                    self.conversation_list.addItem(conv_name)
                # Set the current conversation
                if self.conversations:
                    self.current_conversation = list(self.conversations.keys())[0]
                    self.conversation_history = self.conversations[self.current_conversation]
                else:
                    self.current_conversation = "Chat 1"
                    self.conversation_history = []
            except Exception as e:
                print("Error loading conversations:", e)
        else:
            # No file yet; start with a default conversation.
            self.conversations = {"Chat 1": []}
            self.current_conversation = "Chat 1"
            self.conversation_list.clear()
            self.conversation_list.addItem("Chat 1")

    def save_conversations(self):
        """Save current conversations to a JSON file."""
        try:
            with open("conversations.json", "w", encoding="utf-8") as f:
                json.dump(self.conversations, f, indent=4)
        except Exception as e:
            print("Error saving conversations:", e)

    def closeEvent(self, event):
        self.save_conversations()
        event.accept()
        
    def toggle_recording(self):
        if not self.record_button.isChecked():
            # Stop recording
            self.recorder.stop_recording()
            self.recording_timer.stop()
            self.pause_button.setEnabled(False)
            self.record_button.setIcon(QIcon("assets/icons/mic.svg"))
            self.time_label.setText("00:00")
        else:
            # Start recording
            self.recording_file = tempfile.mktemp(suffix='.wav')
            self.recorder = AudioRecorder()
            self.recorder.setup_recording(self.recording_file)
            self.recorder.finished.connect(self.on_recording_finished)
            self.recorder.start()
            
            self.start_time = datetime.datetime.now()
            self.pause_start = None
            self.recording_timer.start(1000)
            self.pause_button.setEnabled(True)
            self.record_button.setIcon(QIcon("assets/icons/stop-circle.svg"))

    def toggle_pause(self):
        if self.recorder.is_paused:
            self.recorder.resume()
            self.pause_button.setIcon(QIcon("assets/icons/pause.svg"))
            # Update time after pause
            if self.pause_start:
                pause_duration = datetime.datetime.now() - self.pause_start
                self.start_time += pause_duration
                self.pause_start = None
        else:
            self.recorder.pause()
            self.pause_button.setIcon(QIcon("assets/icons/play.svg"))
            self.pause_start = datetime.datetime.now()

    def update_recording_time(self):
        if self.start_time and not self.recorder.is_paused:
            delta = datetime.datetime.now() - self.start_time
            if self.pause_start:
                delta -= datetime.datetime.now() - self.pause_start
            self.time_label.setText(str(delta).split('.')[0])

    def on_recording_finished(self, file_path):
        # Change the cursor to the waiting state (clock icon) before transcription starts
        QApplication.setOverrideCursor(self.waiting_cursor)
        
        # Start transcription with language
        language = None if self.language_combo.currentText() == "Auto" else self.language_combo.currentText()
        self.transcription_worker = TranscriptionWorker(
            file_path, 
            self.model_combo.currentText(),
            language
        )
        self.transcription_worker.finished.connect(self.handle_transcription)
        self.transcription_worker.start()

    def handle_transcription(self, text):
        # Restore the normal cursor after transcription completes
        QApplication.restoreOverrideCursor()
        
        if not text.startswith("Error"):
            # Append the transcribed text to existing content instead of replacing
            current_text = self.chat_input.toPlainText()
            if current_text:
                # Add a space between existing text and new text
                self.chat_input.setPlainText(current_text + " " + text)
            else:
                self.chat_input.setPlainText(text)
        else:
            QMessageBox.warning(self, "Transcription Error", text)
        
class AudioRecorder(QThread):
    finished = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.is_recording = False
        self.is_paused = False
        self.output_file = ""
        self.start_time = None

    def setup_recording(self, output_file):
        self.output_file = output_file
        self.is_recording = True
        self.is_paused = False
        self.start_time = datetime.datetime.now()

    def run(self):
        FORMAT = pyaudio.paInt16
        CHANNELS = 1
        RATE = 16000
        CHUNK = 1024
        
        audio = pyaudio.PyAudio()
        stream = audio.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
        frames = []
        
        while self.is_recording:
            data = stream.read(CHUNK)
            if not self.is_paused:
                frames.append(data)
            self.msleep(10)
            
        stream.stop_stream()
        stream.close()
        audio.terminate()
        
        if frames:
            wf = wave.open(self.output_file, 'wb')
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(audio.get_sample_size(FORMAT))
            wf.setframerate(RATE)
            wf.writeframes(b''.join(frames))
            wf.close()
            self.finished.emit(self.output_file)

    def stop_recording(self):
        self.is_recording = False

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False

class TranscriptionWorker(QThread):
    finished = pyqtSignal(str)
    
    def __init__(self, file_path, model_name="tiny", language=None):
        super().__init__()
        self.file_path = file_path
        self.model_name = model_name
        self.language = language

    def run(self):
        try:
            model = whisper.load_model(self.model_name)
            result = model.transcribe(self.file_path, language=self.language)
            self.finished.emit(result["text"])
        except Exception as e:
            self.finished.emit(f"Error: {str(e)}")

# For testing standalone
if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication
    import sys
    app = QApplication(sys.argv)
    window = WorkshopWindow()
    window.show()
    sys.exit(app.exec_())
