import os
import json
import zipfile
import shutil

from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, Button, DataTable, Log, Label
from textual import on


def get_mod_environment(jar_path: str) -> str:
    """
    Inspects a .jar archive for Fabric or Quilt mod metadata.
    Returns:
        'client' - Client-side only
        'server' - Server-side only
        '*'      - Universal (Both client and server)
    """
    try:
        with zipfile.ZipFile(jar_path, 'r') as jar:
            if 'fabric.mod.json' in jar.namelist():
                with jar.open('fabric.mod.json') as f:
                    data = json.load(f)
                    return data.get('environment', '*')

            if 'quilt.mod.json' in jar.namelist():
                with jar.open('quilt.mod.json') as f:
                    data = json.load(f)
                    if 'quilt_loader' in data and 'environment' in data['quilt_loader']:
                        return data['quilt_loader']['environment']
                    return data.get('environment', '*')

            for file_info in jar.infolist():
                if file_info.filename.endswith('.json') and '/' not in file_info.filename:
                    try:
                        with jar.open(file_info.filename) as f:
                            data = json.load(f)
                            if isinstance(data, dict) and 'environment' in data:
                                return data['environment']
                    except Exception:
                        continue

    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError):
        pass

    return '*'


class ExportModal(ModalScreen[str]):
    """Modal dialog asking the user for an export target directory."""
    
    CSS = """
    ExportModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #modal_dialog {
        padding: 1 2;
        width: 70;
        height: 13;
        border: thick $accent;
        background: $surface;
    }

    #modal_title {
        text-style: bold;
        margin-bottom: 1;
    }

    #modal_input {
        margin-top: 1;
        margin-bottom: 1;
    }

    #modal_buttons {
        height: 3;
        align: right middle;
    }

    .modal_btn {
        margin-left: 1;
    }
    """

    def __init__(self, target_type: str, default_path: str):
        super().__init__()
        self.target_type = target_type
        self.default_path = default_path

    def compose(self) -> ComposeResult:
        with Vertical(id="modal_dialog"):
            yield Label(f"Export Destination ({self.target_type.upper()} Pack)", id="modal_title")
            yield Label("Enter directory path to save exported mods:")
            yield Input(value=self.default_path, id="modal_input", placeholder="Enter path...")
            with Horizontal(id="modal_buttons"):
                yield Button("Cancel", id="cancel_btn", variant="error", classes="modal_btn")
                yield Button("Confirm Export", id="confirm_btn", variant="success", classes="modal_btn")

    @on(Button.Pressed, "#confirm_btn")
    def on_confirm(self) -> None:
        path = self.query_one("#modal_input", Input).value.strip('"\' ')
        self.dismiss(path)

    @on(Button.Pressed, "#cancel_btn")
    def on_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#modal_input")
    def on_submit(self) -> None:
        path = self.query_one("#modal_input", Input).value.strip('"\' ')
        self.dismiss(path)


class MCMPES(App):
    TITLE = "MCMPES (Minecraft Modpack Environment Sorter)"

    CSS = """
    Screen {
        layout: vertical;
        padding: 1;
    }

    #path_container {
        height: 3;
        margin-bottom: 1;
    }

    #dir_input {
        width: 80%;
    }

    #scan_btn {
        width: 20%;
    }

    #actions_container {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }

    .action_btn {
        margin-right: 1;
        width: 1fr;
    }

    DataTable {
        height: 1fr;
        border: solid $accent;
    }

    #log_panel {
        height: 8;
        border: solid $secondary;
        margin-top: 1;
    }

    #stats_label {
        margin-top: 1;
        text-style: bold;
    }
    """

    def __init__(self):
        super().__init__()
        self.scanned_mods = []  # Tuples: (filename, env, full_path)

    def compose(self) -> ComposeResult:
        with Horizontal(id="path_container"):
            yield Input(placeholder="Enter directory containing .jar mods...", id="dir_input", value=os.getcwd())
            yield Button("Scan Directory", id="scan_btn", variant="primary")

        yield DataTable(id="mod_table")
        yield Label("Scan a directory to see mod breakdown.", id="stats_label")

        with Horizontal(id="actions_container"):
            yield Button("Export Client Pack (0)", id="export_client_btn", variant="success", classes="action_btn")
            yield Button("Export Server Pack (0)", id="export_server_btn", variant="warning", classes="action_btn")
            yield Button("Export Both (0)", id="export_both_btn", variant="error", classes="action_btn")

        yield Log(id="log_panel")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#mod_table", DataTable)
        table.add_column("Filename", key="filename")
        table.add_column("Environment", key="env")
        table.add_column("Destination Folder(s)", key="dest")
        table.cursor_type = "row"
        
        log = self.query_one("#log_panel", Log)
        log.write_line("Application ready. Enter a directory path and click 'Scan Directory'.")

    def update_export_buttons(self) -> None:
        """Updates the export button labels with the number of files each will export."""
        client_total = sum(1 for _, env, _ in self.scanned_mods if env in ("client", "*"))
        server_total = sum(1 for _, env, _ in self.scanned_mods if env in ("server", "*"))
        both_total = client_total + server_total

        self.query_one("#export_client_btn", Button).label = f"Export Client Pack ({client_total})"
        self.query_one("#export_server_btn", Button).label = f"Export Server Pack ({server_total})"
        self.query_one("#export_both_btn", Button).label = f"Export Both ({both_total})"

    def scan_directory(self) -> None:
        log = self.query_one("#log_panel", Log)
        dir_input = self.query_one("#dir_input", Input).value.strip('"\' ')
        table = self.query_one("#mod_table", DataTable)
        stats_label = self.query_one("#stats_label", Label)

        table.clear()
        self.scanned_mods = []
        self.update_export_buttons()

        if not os.path.exists(dir_input) or not os.path.isdir(dir_input):
            log.write_line(f"[ERROR] Directory does not exist: '{dir_input}'")
            stats_label.update("[ERROR] Invalid directory specified.")
            return

        target_dir = os.path.abspath(dir_input)
        jar_files = [
            f for f in os.listdir(target_dir)
            if f.endswith('.jar') and os.path.isfile(os.path.join(target_dir, f))
        ]

        if not jar_files:
            log.write_line(f"[INFO] No .jar files found in '{target_dir}'.")
            stats_label.update("No .jar files found.")
            return

        log.write_line(f"[INFO] Scanning {len(jar_files)} .jar file(s) in '{target_dir}'...")

        client_count = 0
        server_count = 0
        universal_count = 0

        for filename in jar_files:
            full_path = os.path.join(target_dir, filename)
            env = get_mod_environment(full_path)
            self.scanned_mods.append((filename, env, full_path))

            if env == 'client':
                env_display = "Client Only"
                dest_display = "client_mods/"
                client_count += 1
            elif env == 'server':
                env_display = "Server Only"
                dest_display = "server_mods/"
                server_count += 1
            else:
                env_display = "Universal (*)"
                dest_display = "client_mods/ & server_mods/"
                universal_count += 1

            table.add_row(filename, env_display, dest_display)

        self.update_export_buttons()

        summary = f"Found {len(jar_files)} mods -> Client-Only: {client_count} | Server-Only: {server_count} | Universal: {universal_count}"
        stats_label.update(summary)
        log.write_line(f"[SUCCESS] Scan complete. {summary}")

    def prompt_export(self, export_target: str) -> None:
        log = self.query_one("#log_panel", Log)
        if not self.scanned_mods:
            log.write_line("[WARNING] No mods scanned yet! Click 'Scan Directory' first.")
            return

        dir_input = self.query_one("#dir_input", Input).value.strip('"\' ')
        default_export = os.path.abspath(dir_input) if dir_input else os.getcwd()

        def handle_export_path(output_dir: str | None) -> None:
            if output_dir:
                self.export_mods(export_target, output_dir)

        self.push_screen(ExportModal(export_target, default_export), handle_export_path)

    def export_mods(self, export_target: str, output_dir: str) -> None:
        log = self.query_one("#log_panel", Log)
        base_dir = os.path.abspath(output_dir)
        client_dir = os.path.join(base_dir, "client_mods")
        server_dir = os.path.join(base_dir, "server_mods")

        copied_count = 0

        if export_target in ("client", "both"):
            os.makedirs(client_dir, exist_ok=True)
        if export_target in ("server", "both"):
            os.makedirs(server_dir, exist_ok=True)

        log.write_line(f"[ACTION] Starting export (Target: {export_target.upper()}) to '{base_dir}'...")

        for filename, env, full_path in self.scanned_mods:
            if export_target in ("client", "both"):
                if env in ("client", "*"):
                    dest = os.path.join(client_dir, filename)
                    shutil.copy2(full_path, dest)
                    copied_count += 1
                    log.write_line(f" -> Copied {filename} to client_mods/")

            if export_target in ("server", "both"):
                if env in ("server", "*"):
                    dest = os.path.join(server_dir, filename)
                    shutil.copy2(full_path, dest)
                    copied_count += 1
                    log.write_line(f" -> Copied {filename} to server_mods/")

        log.write_line(f"[SUCCESS] Export complete! Executed {copied_count} file copy operation(s) into '{base_dir}'.")

    @on(Button.Pressed)
    def handle_button_clicks(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "scan_btn":
            self.scan_directory()
        elif button_id == "export_client_btn":
            self.prompt_export("client")
        elif button_id == "export_server_btn":
            self.prompt_export("server")
        elif button_id == "export_both_btn":
            self.prompt_export("both")

    @on(Input.Submitted, "#dir_input")
    def handle_input_submit(self) -> None:
        self.scan_directory()


if __name__ == "__main__":
    app = MCMPES()
    app.run()