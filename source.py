import os
import re
import json
import zipfile
import shutil
from dataclasses import dataclass, field

from textual import on, work
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, Button, DataTable, Log, Label


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

CAT_CLIENT = "client"   # Client only
CAT_SERVER = "server"   # Server only
CAT_EITHER = "either"   # Client OR server (works on either, other side not required)
CAT_BOTH = "both"       # Client AND server (required on both)

CATEGORY_LABELS = {
    CAT_CLIENT: "Client Only",
    CAT_SERVER: "Server Only",
    CAT_EITHER: "Client or Server",
    CAT_BOTH: "Client AND Server",
}

# Which categories go into which pack
CLIENT_PACK_CATS = {CAT_CLIENT, CAT_EITHER, CAT_BOTH}
SERVER_PACK_CATS = {CAT_SERVER, CAT_EITHER, CAT_BOTH}

# Entrypoint key names (Fabric / Quilt) grouped by side
COMMON_EP_KEYS = {"main", "preLaunch", "init"}
CLIENT_EP_KEYS = {"client", "client_init"}
SERVER_EP_KEYS = {"server", "server_init"}

# --- Bytecode signatures -----------------------------------------------------
# Released Fabric jars are usually remapped to *intermediary* names
# (net/minecraft/class_310 = MinecraftClient, class_3176 = DedicatedServer),
# so "net/minecraft/client/" alone only matches Mojmap/Yarn/unobfuscated jars.
# We check both naming schemes.
CLIENT_RE = re.compile(
    rb"net/minecraft/client/"
    rb"|net/fabricmc/fabric/api/client/"
    rb"|net/fabricmc/api/ClientModInitializer"
    rb"|net/minecraft/class_310(?![0-9])"
)
SERVER_RE = re.compile(
    rb"net/minecraft/server/dedicated/"
    rb"|net/fabricmc/api/DedicatedServerModInitializer"
    rb"|net/minecraft/class_3176(?![0-9])"
)
# Content registration: Registry.register (intermediary method_10230), Fabric
# object builders / item groups, or Mojmap/Yarn Registry + "register".
REGISTRY_RE = re.compile(
    rb"method_10230(?![0-9])"
    rb"|net/fabricmc/fabric/api/object/builder/v1/"
    rb"|net/fabricmc/fabric/api/itemgroup/"
)
NAMED_REGISTRY_RE = re.compile(rb"net/minecraft/(?:core|registry)/Registry(?![A-Za-z0-9_])")
# Custom networking protocol
NETWORK_RE = re.compile(
    rb"net/fabricmc/fabric/api/networking/v1/"
    rb"(?:PayloadTypeRegistry|ServerPlayNetworking|ClientPlayNetworking)"
)


@dataclass
class ModInfo:
    category: str
    declared: str = "-"
    reasons: list = field(default_factory=list)

    @property
    def why(self) -> str:
        return "; ".join(self.reasons)


def _strip_json_extras(text: str) -> str:
    """Remove // and /* */ comments (outside strings) and trailing commas."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_json(jar: zipfile.ZipFile, name: str) -> dict:
    """Lenient JSON loader, mirroring how Fabric Loader tolerates sloppy metadata."""
    text = jar.read(name).decode("utf-8-sig")
    try:
        # strict=False allows raw control characters (newlines/tabs) in strings
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        return json.loads(_strip_json_extras(text), strict=False)


def _normalize_env(value) -> str:
    value = str(value).lower() if value is not None else "*"
    if value == "client":
        return "client"
    if value in ("server", "dedicated_server"):
        return "server"
    return "*"


def _read_metadata(jar: zipfile.ZipFile, names: set):
    """
    Returns (loader, declared_env, entrypoint_keys, mixin_configs) or None.
    mixin_configs is a list of (config_filename, environment).
    """
    if "fabric.mod.json" in names:
        data = _load_json(jar, "fabric.mod.json")
        declared = _normalize_env(data.get("environment", "*"))
        eps = {k for k, v in (data.get("entrypoints") or {}).items() if v}

        mixins = []
        for m in data.get("mixins") or []:
            if isinstance(m, str):
                mixins.append((m, "*"))
            elif isinstance(m, dict) and "config" in m:
                mixins.append((m["config"], _normalize_env(m.get("environment", "*"))))
        return "fabric", declared, eps, mixins

    if "quilt.mod.json" in names:
        data = _load_json(jar, "quilt.mod.json")
        loader = data.get("quilt_loader", {})
        declared = _normalize_env(data.get("minecraft", {}).get("environment", "*"))
        eps = {k for k, v in (loader.get("entrypoints") or {}).items() if v}

        raw = data.get("mixin")
        if isinstance(raw, str):
            raw = [raw]
        mixins = [(m, "*") for m in (raw or []) if isinstance(m, str)]
        return "quilt", declared, eps, mixins

    return None


def _count_mixins(jar: zipfile.ZipFile, names: set, configs: list):
    """Returns (common, client, server) mixin class counts."""
    common = client = server = 0
    for cfg, env in configs:
        if cfg not in names:
            continue
        try:
            data = _load_json(jar, cfg)
        except Exception:
            continue
        c = len(data.get("mixins", []))
        cl = len(data.get("client", []))
        s = len(data.get("server", []))
        if env == "client":
            client += c + cl
        elif env == "server":
            server += c + s
        else:
            common += c
            client += cl
            server += s
    return common, client, server


def _scan_bytecode(jar: zipfile.ZipFile, names: set) -> dict:
    """Scan class constant pools (plain byte search works for ASCII names)."""
    result = {"client": 0, "server": 0, "registers": False, "network": False}
    for name in names:
        # skip non-classes, nested jars' contents, and multi-release copies
        if not name.endswith(".class") or name.startswith("META-INF/"):
            continue
        data = jar.read(name)
        if CLIENT_RE.search(data):
            result["client"] += 1
        if SERVER_RE.search(data):
            result["server"] += 1
        if not result["registers"]:
            if REGISTRY_RE.search(data) or (
                NAMED_REGISTRY_RE.search(data) and b"register" in data
            ):
                result["registers"] = True
        if not result["network"] and NETWORK_RE.search(data):
            result["network"] = True
    return result


def analyze_mod(jar_path: str) -> ModInfo:
    """
    Classifies a Fabric/Quilt mod jar into one of:
        client  - client only
        server  - server only
        either  - works on client or server, other side not required
        both    - must be installed on client AND server

    1. fabric.mod.json / quilt.mod.json 'environment' (hard restriction)
    2. Entrypoints + mixin configs (which side has real code)
    3. Bytecode scan (client/server class refs, registry use, custom networking)
    """
    try:
        with zipfile.ZipFile(jar_path, "r") as jar:
            names = set(jar.namelist())
            try:
                meta = _read_metadata(jar, names)
            except json.JSONDecodeError:
                # Last resort: pull "environment" straight out of the raw text
                meta_name = "fabric.mod.json" if "fabric.mod.json" in names else "quilt.mod.json"
                raw = jar.read(meta_name).decode("utf-8-sig", errors="replace")
                m = re.search(r'"environment"\s*:\s*"([^"]*)"', raw)
                env = _normalize_env(m.group(1)) if m else "*"
                if env in ("client", "server"):
                    cat = CAT_CLIENT if env == "client" else CAT_SERVER
                    return ModInfo(cat, env, [f"{meta_name} unparseable; environment found via regex"])
                return ModInfo(
                    CAT_EITHER, "?",
                    [f"{meta_name} unparseable and no environment found - REVIEW MANUALLY"],
                )

            if meta is None:
                return ModInfo(
                    CAT_EITHER, "-",
                    ["No Fabric/Quilt metadata (Forge/NeoForge or library?) - assuming either"],
                )

            loader, declared, eps, mixin_cfgs = meta

            # --- Step 1: declared environment is authoritative -----------
            if declared == "client":
                return ModInfo(CAT_CLIENT, declared, [f"{loader}: environment=client"])
            if declared == "server":
                return ModInfo(CAT_SERVER, declared, [f"{loader}: environment=server"])

            # --- Step 2: entrypoints and mixins --------------------------
            common_eps = eps & COMMON_EP_KEYS
            client_eps = eps & CLIENT_EP_KEYS
            server_eps = eps & SERVER_EP_KEYS
            m_common, m_client, m_server = _count_mixins(jar, names, mixin_cfgs)

            # --- Step 3: bytecode ----------------------------------------
            scan = _scan_bytecode(jar, names)

            has_common = bool(common_eps) or m_common > 0
            has_client = bool(client_eps) or m_client > 0 or scan["client"] > 0
            has_server = bool(server_eps) or m_server > 0 or scan["server"] > 0

            reasons = ["environment=*"]
            if eps:
                reasons.append("entrypoints: " + ",".join(sorted(eps)))
            if mixin_cfgs:
                reasons.append(f"mixins c/cl/s: {m_common}/{m_client}/{m_server}")
            if scan["client"]:
                reasons.append(f"{scan['client']} class(es) use client code")
            if scan["server"]:
                reasons.append(f"{scan['server']} class(es) use dedicated-server code")

            # Declared '*' but nothing runs on the common side -> really one-sided
            if not has_common and has_client and not has_server:
                reasons.append("no common code => client only")
                return ModInfo(CAT_CLIENT, declared, reasons)
            if not has_common and has_server and not has_client:
                reasons.append("no common code => server only")
                return ModInfo(CAT_SERVER, declared, reasons)

            # Common code that changes shared state needs both sides
            if scan["registers"]:
                reasons.append("registers content")
                return ModInfo(CAT_BOTH, declared, reasons)
            if scan["network"]:
                reasons.append("custom networking")
                return ModInfo(CAT_BOTH, declared, reasons)

            if not (has_common or has_client or has_server):
                reasons.append("no code (library/data)")
            return ModInfo(CAT_EITHER, declared, reasons)

    except (zipfile.BadZipFile, OSError, KeyError, json.JSONDecodeError) as e:
        return ModInfo(CAT_EITHER, "-", [f"Could not read jar ({type(e).__name__}) - assuming either"])


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

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
        self.scanned_mods = []  # Tuples: (filename, ModInfo, full_path)

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
        table.add_column("Declared", key="declared")
        table.add_column("Detected", key="detected")
        table.add_column("Destination Folder(s)", key="dest")
        table.add_column("Why", key="why")
        table.cursor_type = "row"

        log = self.query_one("#log_panel", Log)
        log.write_line("Application ready. Enter a directory path and click 'Scan Directory'.")

    def update_export_buttons(self) -> None:
        """Updates the export button labels with the number of files each will export."""
        client_total = sum(1 for _, info, _ in self.scanned_mods if info.category in CLIENT_PACK_CATS)
        server_total = sum(1 for _, info, _ in self.scanned_mods if info.category in SERVER_PACK_CATS)
        both_total = client_total + server_total

        self.query_one("#export_client_btn", Button).label = f"Export Client Pack ({client_total})"
        self.query_one("#export_server_btn", Button).label = f"Export Server Pack ({server_total})"
        self.query_one("#export_both_btn", Button).label = f"Export Both ({both_total})"

    @staticmethod
    def destination_for(category: str) -> str:
        if category == CAT_CLIENT:
            return "client_mods/"
        if category == CAT_SERVER:
            return "server_mods/"
        return "client_mods/ & server_mods/"

    def scan_directory(self) -> None:
        log = self.query_one("#log_panel", Log)
        scan_btn = self.query_one("#scan_btn", Button)
        if scan_btn.disabled:
            return  # a scan is already running

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
        jar_files = sorted(
            f for f in os.listdir(target_dir)
            if f.endswith('.jar') and os.path.isfile(os.path.join(target_dir, f))
        )

        if not jar_files:
            log.write_line(f"[INFO] No .jar files found in '{target_dir}'.")
            stats_label.update("No .jar files found.")
            return

        log.write_line(f"[INFO] Scanning {len(jar_files)} .jar file(s) in '{target_dir}'...")
        stats_label.update("Scanning...")
        scan_btn.disabled = True
        self.run_scan(target_dir, jar_files)

    @work(thread=True)
    def run_scan(self, target_dir: str, jar_files: list) -> None:
        """Runs in a worker thread so bytecode scanning doesn't freeze the UI."""
        results = []
        for filename in jar_files:
            full_path = os.path.join(target_dir, filename)
            info = analyze_mod(full_path)
            results.append((filename, info, full_path))
            self.call_from_thread(self.add_mod_row, filename, info)
        self.call_from_thread(self.finish_scan, results)

    def add_mod_row(self, filename: str, info: ModInfo) -> None:
        table = self.query_one("#mod_table", DataTable)
        table.add_row(
            filename,
            info.declared,
            CATEGORY_LABELS[info.category],
            self.destination_for(info.category),
            info.why,
        )

    def finish_scan(self, results: list) -> None:
        log = self.query_one("#log_panel", Log)
        stats_label = self.query_one("#stats_label", Label)
        self.scanned_mods = results
        self.update_export_buttons()

        counts = {cat: 0 for cat in CATEGORY_LABELS}
        for _, info, _ in results:
            counts[info.category] += 1

        summary = (
            f"Found {len(results)} mods -> "
            f"Client-Only: {counts[CAT_CLIENT]} | "
            f"Server-Only: {counts[CAT_SERVER]} | "
            f"Client-or-Server: {counts[CAT_EITHER]} | "
            f"Client-AND-Server: {counts[CAT_BOTH]}"
        )
        stats_label.update(summary)
        log.write_line(f"[SUCCESS] Scan complete. {summary}")
        self.query_one("#scan_btn", Button).disabled = False

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

        for filename, info, full_path in self.scanned_mods:
            if export_target in ("client", "both") and info.category in CLIENT_PACK_CATS:
                shutil.copy2(full_path, os.path.join(client_dir, filename))
                copied_count += 1
                log.write_line(f" -> Copied {filename} to client_mods/")

            if export_target in ("server", "both") and info.category in SERVER_PACK_CATS:
                shutil.copy2(full_path, os.path.join(server_dir, filename))
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