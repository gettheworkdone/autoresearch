from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings, normalize_reasoning_label
from .models import TaskState, TaskStatus
from .runner import CodexRunner
from .storage import Storage
from .utils import human_size, safe_filename, split_telegram_text
from .worker import WorkerManager

logger = logging.getLogger(__name__)
REASONING_LEVELS = ("light", "standard", "heavy", "extra")


class AutoresearchBot:
    """Single-user Telegram control surface for autoresearch."""

    COMMANDS: tuple[tuple[str, str], ...] = (
        ("start", "open menu"),
        ("menu", "show buttons"),
        ("help", "show commands"),
        ("newtask", "create task"),
        ("cancelnew", "cancel task wizard"),
        ("tasks", "list/select/delete tasks"),
        ("select", "select task by id"),
        ("deletetask", "delete task"),
        ("resume", "resume selected task"),
        ("pause", "pause after current turn"),
        ("stop", "stop current turn"),
        ("continue", "continue paused batch"),
        ("status", "show task status"),
        ("folder", "show task folder"),
        ("exportdata", "zip core data"),
        ("models", "list/set models"),
        ("setmodel", "change model"),
        ("reasoning", "list/set thinking"),
        ("setreasoning", "change thinking"),
        ("setlimit", "set result limit"),
        ("contextmessages", "show/set message count"),
        ("messages", "manage messages in context"),
        ("uploads", "manage upload metadata"),
        ("skills", "manage skills"),
        ("addskill", "add skill"),
        ("addcred", "add credential"),
        ("credentials", "view credential names"),
        ("goal", "view/edit goal"),
        ("setgoal", "replace goal"),
        ("constitution", "view/edit constitution"),
        ("setconstitution", "replace constitution"),
        ("clearcontext", "clear next context"),
        ("contextmode", "context file/summary/off"),
    )

    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = Storage(settings)
        self.runner = CodexRunner(settings, self.storage)
        self.app: Optional[Application] = None
        self.worker: Optional[WorkerManager] = None

    def build(self) -> Application:
        app = ApplicationBuilder().token(self.settings.telegram_bot_token).post_init(self.post_init).build()
        self.app = app
        self.worker = WorkerManager(self.settings, self.storage, self.runner, self.send_text, self.send_file)

        for command, handler in [
            ("start", self.cmd_start),
            ("menu", self.cmd_menu),
            ("help", self.cmd_help),
            ("newtask", self.cmd_newtask),
            ("cancelnew", self.cmd_cancelnew),
            ("tasks", self.cmd_tasks),
            ("select", self.cmd_select),
            ("deletetask", self.cmd_deletetask),
            ("resume", self.cmd_resume),
            ("pause", self.cmd_pause),
            ("stop", self.cmd_stop),
            ("continue", self.cmd_continue),
            ("status", self.cmd_status),
            ("folder", self.cmd_folder),
            ("exportdata", self.cmd_exportdata),
            ("models", self.cmd_models),
            ("setmodel", self.cmd_setmodel),
            ("reasoning", self.cmd_reasoning),
            ("setreasoning", self.cmd_setreasoning),
            ("setlimit", self.cmd_setlimit),
            ("contextmessages", self.cmd_contextmessages),
            ("messages", self.cmd_messages),
            ("uploads", self.cmd_uploads),
            ("skills", self.cmd_skills),
            ("addcred", self.cmd_addcred),
            ("credentials", self.cmd_credentials),
            ("goal", self.cmd_goal),
            ("setgoal", self.cmd_setgoal),
            ("constitution", self.cmd_constitution),
            ("setconstitution", self.cmd_setconstitution),
            ("clearcontext", self.cmd_clearcontext),
            ("contextmode", self.cmd_contextmode),
            ("addskill", self.cmd_addskill),
        ]:
            app.add_handler(CommandHandler(command, handler))

        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, self.on_file))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)
        return app

    async def post_init(self, app: Application) -> None:
        try:
            commands = [BotCommand(command, description) for command, description in self.COMMANDS]
            await app.bot.set_my_commands(commands)
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            logger.info("Registered Telegram command menu with %s commands", len(commands))
        except TelegramError as exc:
            logger.warning("Could not register Telegram command menu: %s", exc)

    def authorized(self, update: Update) -> bool:
        user = update.effective_user
        if not user or user.id != self.settings.telegram_allowed_user_id:
            logger.warning("Unauthorized update from user=%s", user.id if user else None)
            return False
        return True

    def current_task(self) -> Optional[TaskState]:
        return self.storage.selected_task()

    def resolve_task(self, text: str) -> Optional[TaskState]:
        needle = (text or "").strip().lower()
        if not needle:
            return None
        tasks = self.storage.list_tasks()
        for t in tasks:
            if t.task_id == needle or t.task_id.lower().startswith(needle):
                return t
        exact = [t for t in tasks if t.name.lower() == needle]
        if exact:
            return exact[0]
        contains = [t for t in tasks if needle in t.name.lower()]
        return contains[0] if contains else None

    def menu_markup(self, task: Optional[TaskState]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("➕ New", callback_data="action:newtask"), InlineKeyboardButton("📂 Tasks", callback_data="action:tasks")],
            [InlineKeyboardButton("📊 Status", callback_data="action:status"), InlineKeyboardButton("▶️ Resume", callback_data="action:resume")],
            [InlineKeyboardButton("⏸ Pause", callback_data="action:pause"), InlineKeyboardButton("⏭ Continue", callback_data="action:continue"), InlineKeyboardButton("⏹ Stop", callback_data="action:stop")],
            [InlineKeyboardButton("🧠 Models", callback_data="action:models"), InlineKeyboardButton("⚙️ Manage", callback_data="action:manage")],
            [InlineKeyboardButton("📦 Export", callback_data="action:exportdata"), InlineKeyboardButton("❓ Help", callback_data="action:help")],
        ]
        if task:
            rows.insert(0, [InlineKeyboardButton(f"Selected: {task.name[:45]}", callback_data="action:status")])
        return InlineKeyboardMarkup(rows)

    def manage_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Messages", callback_data="action:messages"), InlineKeyboardButton("📎 Upload metadata", callback_data="action:uploads")],
            [InlineKeyboardButton("🧩 Skills", callback_data="action:skills"), InlineKeyboardButton("🔐 Credentials", callback_data="action:credentials")],
            [InlineKeyboardButton("🎯 Goal", callback_data="action:goal"), InlineKeyboardButton("📜 Constitution", callback_data="action:constitution")],
            [InlineKeyboardButton("🔢 Context messages", callback_data="action:contextmessages"), InlineKeyboardButton("🗑 Delete task", callback_data="action:deletetasks")],
            [InlineKeyboardButton("⬅️ Main menu", callback_data="action:menu")],
        ])

    def start_text(self) -> str:
        task = self.current_task()
        selected = f"\nSelected task: {task.name} ({task.status})" if task else "\nNo task selected."
        return (
            "Autoresearch is running."
            f"{selected}\n\n"
            "Use /newtask to create a task or /tasks to select one. "
            "During setup, upload as many supplementary files as you want. "
            "Iterations start only after you send one normal text message with the initial instruction."
        )

    def help_text(self) -> str:
        return (
            "Commands:\n"
            "/menu - show button menu\n"
            "/newtask - create task\n"
            "/tasks - list, select, delete tasks\n"
            "/deletetask NAME_OR_ID - remove task folder completely\n"
            "/resume - start/resume selected task\n"
            "/pause, /stop, /continue - control loop\n"
            "/status, /folder - inspect current task\n"
            "/exportdata - send ZIP with uploads, goal, constitution, credentials, skills; no messages/workspace\n"
            "/models, /setmodel MODEL - list/change model\n"
            "/reasoning, /setreasoning light|standard|heavy|extra - list/change thinking level\n"
            "/contextmessages [N] - show or set how many Telegram messages go into model context\n"
            "/messages - permanently delete Telegram messages from VPS with buttons\n"
            "/uploads - delete upload metadata from future context with buttons\n"
            "/skills, /addskill - list/add/delete custom skills\n"
            "/credentials - show stored credential names, masked\n"
            "/goal, /setgoal - view/change goal\n"
            "/constitution, /setconstitution - view/change constitution\n"
            "/clearcontext - next turn rebuilds context from task files\n"
            "/contextmode file|summary|off - context snapshot sending\n\n"
            "Files: upload PDFs, ZIPs, images, notebooks, CSVs, code, or logs. "
            "In draft setup, files are saved as supplementary material; send text to start iterations."
        )


    def credentials_text(self, task: TaskState) -> str:
        lines = [self.storage.credential_summary(task)]
        global_lines = []
        for name in self.settings.passthrough_env_names:
            import os
            value = os.getenv(name)
            if value:
                global_lines.append(f"- {name}: set globally, length {len(value)}, prefix {value[:5]!r}...")
        if global_lines:
            lines.append("\nGlobal passthrough credentials from .env (values hidden):")
            lines.extend(global_lines)
        return "\n".join(lines)

    def status_text(self, task: TaskState) -> str:
        running = self.worker.is_running(task.task_id) if self.worker else False
        return (
            f"Task: {task.name}\n"
            f"ID: {task.task_id}\n"
            f"Status: {task.status}\n"
            f"Worker running: {running}\n"
            f"Model: {task.model}\n"
            f"Pending model: {task.pending_model or '-'}\n"
            f"Reasoning: {task.reasoning_effort}\n"
            f"Pending reasoning: {task.pending_reasoning_effort or '-'}\n"
            f"Iteration: {task.iteration}\n"
            f"Result messages in batch: {task.result_messages_sent}/{task.max_result_messages}\n"
            f"Telegram messages in context: {task.context_message_limit}\n"
            f"Folder size: {self.storage.folder_size_human(task)}\n"
            f"Root: {task.root}"
        )

    async def send_text(self, text: str, buttons: Optional[dict] = None) -> None:
        if not self.app:
            return
        keyboard = None
        if buttons:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"action:{key}") for key, label in buttons.items()]])
        for idx, part in enumerate(split_telegram_text(text)):
            await self.app.bot.send_message(
                chat_id=self.settings.telegram_allowed_user_id,
                text=part,
                reply_markup=keyboard if idx == 0 else None,
                disable_web_page_preview=True,
            )

    async def send_file(self, path: str, caption: str) -> None:
        if not self.app:
            return
        p = Path(path)
        try:
            with p.open("rb") as f:
                await self.app.bot.send_document(
                    chat_id=self.settings.telegram_allowed_user_id,
                    document=f,
                    filename=p.name,
                    caption=caption[:1024],
                )
        except Exception as exc:
            await self.send_text(f"Could not send file {p}: {exc}", None)

    async def send_menu(self, text: Optional[str] = None) -> None:
        if not self.app:
            return
        await self.app.bot.send_message(
            chat_id=self.settings.telegram_allowed_user_id,
            text=text or self.start_text(),
            reply_markup=self.menu_markup(self.current_task()),
            disable_web_page_preview=True,
        )

    async def send_tasks_list(self) -> None:
        if not self.app:
            return
        tasks = self.storage.list_tasks()
        if not tasks:
            await self.app.bot.send_message(chat_id=self.settings.telegram_allowed_user_id, text="No tasks yet. Use /newtask.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ New task", callback_data="action:newtask")]]))
            return
        rows = []
        lines = ["Tasks:"]
        selected_id = self.storage.load_state().selected_task_id
        for task in tasks[:20]:
            selected = "*" if selected_id == task.task_id else " "
            running = " running" if self.worker and self.worker.is_running(task.task_id) else ""
            lines.append(f"{selected} {task.task_id} | {task.status}{running} | {task.name}")
            rows.append([
                InlineKeyboardButton(f"📌 {task.name[:32]}", callback_data=f"select:{task.task_id[:50]}"),
                InlineKeyboardButton("🗑", callback_data=f"tdel:{task.task_id[:50]}"),
            ])
        await self.app.bot.send_message(chat_id=self.settings.telegram_allowed_user_id, text="\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            await update.message.reply_text(self.start_text(), reply_markup=self.menu_markup(self.current_task()), disable_web_page_preview=True)

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            await update.message.reply_text(self.start_text(), reply_markup=self.menu_markup(self.current_task()), disable_web_page_preview=True)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            await update.message.reply_text(self.help_text(), reply_markup=self.menu_markup(self.current_task()), disable_web_page_preview=True)

    async def begin_newtask_wizard(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data["wizard"] = {"step": "name", "credentials": {}}

    async def cmd_newtask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        await self.begin_newtask_wizard(context)
        await update.message.reply_text("New task. First send the task name.")

    async def cmd_cancelnew(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        context.user_data.pop("wizard", None)
        context.user_data.pop("edit_goal_task_id", None)
        context.user_data.pop("edit_constitution_task_id", None)
        await update.message.reply_text("Wizard/edit mode cancelled.")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        text = update.message.text or ""
        if context.user_data.get("edit_goal_task_id"):
            await self.handle_goal_edit(update, context, text)
            return
        if context.user_data.get("edit_constitution_task_id"):
            await self.handle_constitution_edit(update, context, text)
            return
        wizard = context.user_data.get("wizard")
        if wizard:
            await self.handle_wizard_text(update, context, text)
            return
        task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task. Use /newtask first.")
            return
        path = self.storage.add_user_message(task, text)
        if task.status in {TaskStatus.DRAFT.value, TaskStatus.PAUSED.value, TaskStatus.STOPPED.value, TaskStatus.ERROR.value}:
            self.storage.write_next_prompt(task, text)
        if task.status == TaskStatus.DRAFT.value and self.settings.auto_start_draft_on_message:
            task.notes["initial_instruction_received"] = True
            self.storage.upsert_task(task, select=True)
            await update.message.reply_text(f"Stored initial text instruction: {path.name}. Starting autoresearch.")
            await self.worker.start(task)
            return
        await update.message.reply_text(f"Stored message for {task.name}: {path.name}. Codex will read it before the next move.")

    async def handle_wizard_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        wizard = context.user_data["wizard"]
        step = wizard.get("step")
        clean = text.strip()
        if step == "name":
            wizard["name"] = clean
            wizard["step"] = "goal"
            await update.message.reply_text("Now send the main goal. This becomes protected GOAL.md.")
            return
        if step == "goal":
            wizard["goal"] = clean
            task = self.storage.create_task(
                name=wizard["name"],
                goal=wizard["goal"],
                model=self.settings.default_model,
                max_result_messages=self.settings.default_max_result_messages,
                resume_codex_session=self.settings.resume_codex_session,
                reasoning_effort=self.settings.default_reasoning_effort,
                context_message_limit=self.settings.default_context_message_limit,
            )
            wizard["task_id"] = task.task_id
            wizard["step"] = "credentials"
            await update.message.reply_text(
                "Task folder created. Send credentials as KEY=value lines, one or many per message. "
                "For Kaggle, use KAGGLE_API_TOKEN=<token>. Type done or skip when finished."
            )
            return
        if step == "credentials":
            task = self.storage.get_task(wizard["task_id"])
            if clean.lower() in {"done", "skip", "no", "none"}:
                wizard["step"] = "model"
                await update.message.reply_text(f"Choose model. Send a model name, or default for {self.settings.default_model}.")
                return
            parsed = 0
            for line in clean.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key:
                    self.storage.add_credential(task, key, value.strip())
                    parsed += 1
            await update.message.reply_text(f"Stored {parsed} credential(s). Send more, or type done.")
            return
        if step == "model":
            task = self.storage.get_task(wizard["task_id"])
            if clean.lower() != "default":
                task.model = clean
            self.storage.upsert_task(task, select=True)
            wizard["step"] = "reasoning"
            await update.message.reply_text("Choose thinking/reasoning level: light, standard, heavy, or extra. Send default for standard.")
            return
        if step == "reasoning":
            task = self.storage.get_task(wizard["task_id"])
            level = self.settings.default_reasoning_effort if clean.lower() == "default" else normalize_reasoning_label(clean)
            if level not in REASONING_LEVELS:
                level = self.settings.default_reasoning_effort
            task.reasoning_effort = level
            self.storage.upsert_task(task, select=True)
            wizard["step"] = "limit"
            await update.message.reply_text(f"How many result messages before automatic pause? Default: {self.settings.default_max_result_messages}.")
            return
        if step == "limit":
            task = self.storage.get_task(wizard["task_id"])
            try:
                n = int(clean)
                if n <= 0:
                    raise ValueError
                task.max_result_messages = n
            except ValueError:
                task.max_result_messages = self.settings.default_max_result_messages
            self.storage.upsert_task(task, select=True)
            wizard["step"] = "context_messages"
            await update.message.reply_text(f"How many latest Telegram messages should be inserted into model context? Default: {self.settings.default_context_message_limit}. Send 0 to include none.")
            return
        if step == "context_messages":
            task = self.storage.get_task(wizard["task_id"])
            try:
                n = int(clean)
                if n < 0:
                    raise ValueError
                task.context_message_limit = n
            except ValueError:
                task.context_message_limit = self.settings.default_context_message_limit
            self.storage.upsert_task(task, select=True)
            wizard["step"] = "initial"
            await update.message.reply_text(
                "Now upload any supplementary files you want: PDFs, ZIPs, images, notebooks, CSVs, logs, etc. "
                "They will be stored and copied into the task workspace.\n\n"
                "Iterations will NOT start from files or captions. When uploads are done, send one TEXT message "
                "with the initial instruction. The autonomous loop starts only after that text instruction."
            )
            return
        if step == "initial":
            task = self.storage.get_task(wizard["task_id"])
            self.storage.add_user_message(task, clean, source="initial")
            self.storage.write_next_prompt(task, clean)
            task.notes["initial_instruction_received"] = True
            self.storage.upsert_task(task, select=True)
            context.user_data.pop("wizard", None)
            await update.message.reply_text("Initial text instruction saved. Starting the autonomous loop.")
            await self.worker.start(task)
            return

    async def handle_goal_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        task = self.storage.get_task(context.user_data.pop("edit_goal_task_id"))
        if not task:
            await update.message.reply_text("Task not found.")
            return
        self.storage.update_goal(task, text)
        await update.message.reply_text("Goal updated. The previous version was backed up.", reply_markup=self.menu_markup(task))

    async def handle_constitution_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        task = self.storage.get_task(context.user_data.pop("edit_constitution_task_id"))
        if not task:
            await update.message.reply_text("Task not found.")
            return
        self.storage.update_constitution(task, text)
        await update.message.reply_text("Constitution updated. The previous version was backed up.", reply_markup=self.menu_markup(task))

    async def cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            await self.send_tasks_list()

    async def cmd_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /select TASK_ID_OR_PREFIX")
            return
        task = self.resolve_task(context.args[0])
        if not task:
            await update.message.reply_text("No matching task.")
            return
        task = self.storage.select_task(task.task_id)
        await update.message.reply_text(f"Selected {task.name} ({task.task_id}).", reply_markup=self.menu_markup(task))

    async def cmd_deletetask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /deletetask NAME_OR_ID. Or use /tasks and tap 🗑.")
            return
        task = self.resolve_task(" ".join(context.args))
        if not task:
            await update.message.reply_text("No matching task.")
            return
        await update.message.reply_text(
            f"Delete task completely from VPS?\n{task.name}\n{task.task_id}\n{task.root}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("YES, DELETE", callback_data=f"tdelyes:{task.task_id[:50]}"), InlineKeyboardButton("Cancel", callback_data="action:tasks")]]),
        )

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task. Use /tasks or /newtask.")
            return
        await self.worker.start(task)

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        await self.worker.pause(task) if task else await update.message.reply_text("No selected task.")

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        await self.worker.stop(task) if task else await update.message.reply_text("No selected task.")

    async def cmd_continue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task.")
            return
        await self.worker.continue_after_limit(task)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        await update.message.reply_text(self.status_text(task), reply_markup=self.menu_markup(task)) if task else await update.message.reply_text("No selected task.")

    async def cmd_folder(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        await update.message.reply_text(f"{task.root}\nWorkspace: {task.root}/workspace\nSize: {self.storage.folder_size_human(task)}") if task else await update.message.reply_text("No selected task.")

    async def cmd_exportdata(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task.")
            return
        path = self.storage.create_data_export_zip(task)
        await update.message.reply_text("Created core-data export ZIP. It excludes Telegram messages, logs, context snapshots, and workspace/sandbox.")
        await self.send_file(str(path), f"Core data export for {task.name}")

    async def cmd_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            await update.message.reply_text(self.models_text(), reply_markup=self.models_markup())

    def models_text(self) -> str:
        task = self.current_task()
        current = task.model if task else "-"
        lines = [f"Configured available models. Current: {current}"]
        for m in self.settings.available_models:
            lines.append(f"- {m}")
        lines.append("\nEdit AVAILABLE_MODELS in /opt/autoresearch/.env if your Codex account exposes more models.")
        return "\n".join(lines)

    def models_markup(self) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(m[:50], callback_data=f"model:{i}")] for i, m in enumerate(self.settings.available_models[:20])]
        rows.append([InlineKeyboardButton("Thinking levels", callback_data="action:reasoning"), InlineKeyboardButton("Menu", callback_data="action:menu")])
        return InlineKeyboardMarkup(rows)

    async def cmd_setmodel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task or not context.args:
            await update.message.reply_text("Usage: /setmodel gpt-5.5")
            return
        await self.apply_model(task, " ".join(context.args).strip(), update.message.reply_text)

    async def apply_model(self, task: TaskState, new_model: str, reply_func) -> None:
        if self.worker and self.worker.is_running(task.task_id):
            task.pending_model = new_model
            self.storage.upsert_task(task, select=True)
            await reply_func(f"Model change queued: {new_model}. It applies after the current turn.")
        else:
            old = task.model
            task.model = new_model
            self.storage.upsert_task(task, select=True)
            await reply_func(f"Model changed: {old} -> {new_model}.")

    async def cmd_reasoning(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            await update.message.reply_text(self.reasoning_text(), reply_markup=self.reasoning_markup())

    def reasoning_text(self) -> str:
        task = self.current_task()
        current = task.reasoning_effort if task else "-"
        return (
            f"Current thinking/reasoning level: {current}\n\n"
            "Available levels:\n"
            "- light: faster, lower reasoning\n"
            "- standard: default balance\n"
            "- heavy: deeper reasoning\n"
            "- extra: maximum available reasoning, slower/costlier"
        )

    def reasoning_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(level, callback_data=f"reason:{level}") for level in REASONING_LEVELS], [InlineKeyboardButton("Models", callback_data="action:models"), InlineKeyboardButton("Menu", callback_data="action:menu")]])

    async def cmd_setreasoning(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task or not context.args:
            await update.message.reply_text("Usage: /setreasoning light|standard|heavy|extra")
            return
        level = normalize_reasoning_label(context.args[0])
        if level not in REASONING_LEVELS:
            await update.message.reply_text("Use: light, standard, heavy, or extra.")
            return
        await self.apply_reasoning(task, level, update.message.reply_text)

    async def apply_reasoning(self, task: TaskState, level: str, reply_func) -> None:
        if self.worker and self.worker.is_running(task.task_id):
            task.pending_reasoning_effort = level
            self.storage.upsert_task(task, select=True)
            await reply_func(f"Reasoning change queued: {level}. It applies after the current turn.")
        else:
            old = task.reasoning_effort
            task.reasoning_effort = level
            self.storage.upsert_task(task, select=True)
            await reply_func(f"Reasoning changed: {old} -> {level}.")

    async def cmd_contextmessages(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if context.args:
            if not task:
                await update.message.reply_text("No selected task.")
                return
            try:
                n = int(context.args[0])
                if n < 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Usage: /contextmessages N, where N is an integer >= 0.")
                return
            task.context_message_limit = n
            self.storage.upsert_task(task, select=True)
            await update.message.reply_text(f"Context message limit set to {n}.", reply_markup=self.contextmessages_markup())
            return
        await update.message.reply_text(self.contextmessages_text(), reply_markup=self.contextmessages_markup())

    def contextmessages_text(self) -> str:
        task = self.current_task()
        current = task.context_message_limit if task else self.settings.default_context_message_limit
        return f"Latest Telegram messages inserted into model context before each turn: {current}"

    def contextmessages_markup(self) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(str(n), callback_data=f"ctxmsg:{n}") for n in (0, 5, 10)], [InlineKeyboardButton(str(n), callback_data=f"ctxmsg:{n}") for n in (30, 50, 100)], [InlineKeyboardButton("Menu", callback_data="action:menu")]]
        return InlineKeyboardMarkup(rows)

    async def cmd_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            await self.send_messages_manager(update.message.reply_text)

    async def send_messages_manager(self, reply_func) -> None:
        task = self.current_task()
        if not task:
            await reply_func("No selected task.")
            return
        all_msgs = self.storage.list_all_messages(task)
        lines = [f"Messages for {task.name}. Tap 🗑 to permanently delete from VPS and future model context."]
        rows = []
        for p in all_msgs[-20:]:
            prefix = p.name[:6]
            preview = p.read_text(encoding="utf-8", errors="replace").splitlines()[-1][:60] if p.exists() else ""
            lines.append(f"{prefix} {preview}")
            rows.append([InlineKeyboardButton(f"🗑 {prefix}", callback_data=f"msgdel:{prefix}"), InlineKeyboardButton("Refresh", callback_data="action:messages")])
        if not rows:
            rows = [[InlineKeyboardButton("Menu", callback_data="action:menu")]]
        await reply_func("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_uploads(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            await self.send_uploads_manager(update.message.reply_text)

    async def send_uploads_manager(self, reply_func) -> None:
        task = self.current_task()
        if not task:
            await reply_func("No selected task.")
            return
        metas = self.storage.list_upload_metadata(task, include_excluded=True)
        lines = [f"Upload metadata for {task.name}. Tap 🗑 to delete metadata from future model context. Uploaded files remain saved."]
        rows = []
        for p in metas[-20:]:
            prefix = p.name[:6]
            try:
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                label = data.get("original_name", p.name)
                size = human_size(int(data.get("size_bytes", 0)))
            except Exception:
                label, size = p.name, "?"
            lines.append(f"{prefix} {label} ({size})")
            rows.append([InlineKeyboardButton(f"🗑 {prefix}", callback_data=f"updel:{prefix}"), InlineKeyboardButton("Refresh", callback_data="action:uploads")])
        if not rows:
            rows = [[InlineKeyboardButton("Menu", callback_data="action:menu")]]
        await reply_func("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            await self.send_skills_manager(update.message.reply_text)

    async def send_skills_manager(self, reply_func) -> None:
        task = self.current_task()
        if not task:
            await reply_func("No selected task.")
            return
        skills = self.storage.list_custom_skills(task)
        lines = [f"Custom skills for {task.name}. Tap 🗑 to delete."]
        rows = []
        for p in skills[:30]:
            token = p.stem[:32]
            lines.append(f"- {p.name}")
            rows.append([InlineKeyboardButton(f"🗑 {p.name[:40]}", callback_data=f"skilldel:{token}")])
        rows.append([InlineKeyboardButton("Menu", callback_data="action:menu")])
        await reply_func("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_setlimit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task or not context.args:
            await update.message.reply_text("Usage: /setlimit 50")
            return
        try:
            n = int(context.args[0])
            if n <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("N must be a positive integer.")
            return
        task.max_result_messages = n
        self.storage.upsert_task(task, select=True)
        await update.message.reply_text(f"Limit set to {n} result messages per batch.")

    async def cmd_clearcontext(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task.")
            return
        task.clear_context_requested = True
        task.resume_codex_session = False
        self.storage.upsert_task(task, select=True)
        await update.message.reply_text("Clear-context requested. The next turn rebuilds context from task files and does not resume a Codex session.")

    async def cmd_addcred(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task or not context.args:
            await update.message.reply_text("Usage: /addcred KEY=value. For Kaggle use /addcred KAGGLE_API_TOKEN=...")
            return
        raw = " ".join(context.args)
        if "=" not in raw:
            await update.message.reply_text("Usage: /addcred KEY=value")
            return
        key, value = raw.split("=", 1)
        self.storage.add_credential(task, key.strip(), value.strip())
        await update.message.reply_text(f"Stored credential variable {key.strip()} for this task.")

    async def cmd_credentials(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        await update.message.reply_text(self.credentials_text(task), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Export core data ZIP", callback_data="action:exportdata"), InlineKeyboardButton("Menu", callback_data="action:menu")]])) if task else await update.message.reply_text("No selected task.")

    async def cmd_addskill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task or len(context.args) < 2:
            await update.message.reply_text("Usage: /addskill skill-name instructions...")
            return
        name = context.args[0]
        content = " ".join(context.args[1:])
        path = self.storage.add_custom_skill(task, name, content)
        await update.message.reply_text(f"Stored custom skill: {path.name}")

    async def cmd_goal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task.")
            return
        p = self.storage.task_paths(task)["goal"]
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else "(missing goal)"
        await update.message.reply_text(f"Current goal:\n\n{text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Edit goal", callback_data="edit:goal"), InlineKeyboardButton("Menu", callback_data="action:menu")]]))

    async def cmd_setgoal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task.")
            return
        if context.args:
            self.storage.update_goal(task, " ".join(context.args))
            await update.message.reply_text("Goal updated.")
        else:
            context.user_data["edit_goal_task_id"] = task.task_id
            await update.message.reply_text("Send the new full goal text. It will replace GOAL.md. Previous goal will be backed up. Use /cancelnew to cancel.")

    async def cmd_constitution(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task.")
            return
        p = self.storage.task_paths(task)["constitution"]
        await self.send_file(str(p), f"Current constitution for {task.name}")
        await update.message.reply_text("Use the button to replace the constitution.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Edit constitution", callback_data="edit:constitution"), InlineKeyboardButton("Menu", callback_data="action:menu")]]))

    async def cmd_setconstitution(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task.")
            return
        if context.args:
            self.storage.update_constitution(task, " ".join(context.args))
            await update.message.reply_text("Constitution updated.")
        else:
            context.user_data["edit_constitution_task_id"] = task.task_id
            await update.message.reply_text("Send the new full constitution text. It will replace CONSTITUTION.md. Previous version will be backed up. Use /cancelnew to cancel.")

    async def cmd_contextmode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        if not context.args or context.args[0] not in {"file", "summary", "off"}:
            await update.message.reply_text("Usage: /contextmode file|summary|off. Restart service to make .env default permanent.")
            return
        object.__setattr__(self.settings, "send_context_mode", context.args[0])
        await update.message.reply_text(f"Context sending mode set to {context.args[0]} for this process.")

    async def on_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        wizard = context.user_data.get("wizard")
        task: Optional[TaskState] = None
        if wizard and wizard.get("task_id"):
            task = self.storage.get_task(wizard["task_id"])
        if task is None:
            task = self.current_task()
        if not task:
            await update.message.reply_text("No selected task. Use /newtask first, then send files.")
            return

        message = update.message
        tg_file = None
        original_name = "upload"
        size = None
        if message.document:
            doc = message.document
            tg_file = await doc.get_file()
            original_name = doc.file_name or f"document_{doc.file_unique_id}"
            size = doc.file_size
        elif message.photo:
            photo = message.photo[-1]
            tg_file = await photo.get_file()
            original_name = f"photo_{photo.file_unique_id}.jpg"
            size = photo.file_size
        if tg_file is None:
            return

        max_bytes = self.settings.max_upload_mb * 1024 * 1024
        if size and size > max_bytes:
            await update.message.reply_text(f"Upload is {human_size(size)}, above configured limit {self.settings.max_upload_mb} MB.")
            return

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / safe_filename(original_name)
            await tg_file.download_to_drive(custom_path=str(tmp))
            if tmp.stat().st_size > max_bytes:
                await update.message.reply_text(f"Upload is {human_size(tmp.stat().st_size)}, above configured limit {self.settings.max_upload_mb} MB.")
                return
            try:
                stored, created, note = self.storage.add_uploaded_file(task, tmp, original_name, self.settings.max_zip_total_mb, self.settings.max_zip_files)
            except Exception as exc:
                await update.message.reply_text(f"Upload store failed: {exc}")
                return

        caption = (message.caption or "").strip()
        if caption:
            self.storage.add_user_message(task, caption, source=f"caption:{safe_filename(original_name)}")
            if task.status in {TaskStatus.PAUSED.value, TaskStatus.STOPPED.value, TaskStatus.ERROR.value}:
                self.storage.write_next_prompt(task, caption)

        if original_name.lower().endswith(".md") and ("skill" in original_name.lower() or caption.lower().startswith("/skill")):
            content = stored.read_text(encoding="utf-8", errors="replace")
            self.storage.add_custom_skill(task, Path(original_name).stem, content)
            note += "; also installed as custom skill"

        await update.message.reply_text(f"📎 {note}. Workspace copies: {len(created)}. Folder size: {self.storage.folder_size_human(task)}")
        if wizard and wizard.get("step") == "initial":
            await update.message.reply_text("File saved as supplementary material. Upload more files if needed. When ready, send one TEXT message with the initial instruction to start iterations.")
            return
        if task.status == TaskStatus.DRAFT.value:
            await update.message.reply_text("Draft task has not started. Files/captions are stored, but iterations start only after a text instruction.")

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        query = update.callback_query
        await query.answer()
        data = query.data or ""

        if data.startswith("select:"):
            task = self.resolve_task(data.split(":", 1)[1])
            if task:
                task = self.storage.select_task(task.task_id)
                await query.edit_message_text(f"Selected {task.name} ({task.task_id}).", reply_markup=self.menu_markup(task))
            else:
                await query.edit_message_text("Task not found.")
            return

        if data.startswith("tdel:"):
            task = self.resolve_task(data.split(":", 1)[1])
            if not task:
                await query.edit_message_text("Task not found.")
                return
            await query.edit_message_text(f"Delete task completely from VPS?\n{task.name}\n{task.task_id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("YES, DELETE", callback_data=f"tdelyes:{task.task_id[:50]}"), InlineKeyboardButton("Cancel", callback_data="action:tasks")]]))
            return

        if data.startswith("tdelyes:"):
            task = self.resolve_task(data.split(":", 1)[1])
            if not task:
                await query.edit_message_text("Task not found or already deleted.")
                return
            if self.worker and self.worker.is_running(task.task_id):
                await self.worker.stop(task)
            name = task.name
            ok = self.storage.delete_task(task.task_id)
            await query.edit_message_text(f"Deleted task {name}." if ok else "Delete failed or task was already gone.")
            return

        task = self.current_task()

        if data.startswith("model:"):
            if not task:
                await query.edit_message_text("No selected task.")
                return
            idx = int(data.split(":", 1)[1])
            model = self.settings.available_models[idx]
            await self.apply_model(task, model, query.message.reply_text)
            return

        if data.startswith("reason:"):
            if not task:
                await query.edit_message_text("No selected task.")
                return
            await self.apply_reasoning(task, data.split(":", 1)[1], query.message.reply_text)
            return

        if data.startswith("ctxmsg:"):
            if not task:
                await query.edit_message_text("No selected task.")
                return
            task.context_message_limit = int(data.split(":", 1)[1])
            self.storage.upsert_task(task, select=True)
            await query.edit_message_text(self.contextmessages_text(), reply_markup=self.contextmessages_markup())
            return

        if data.startswith("msgdel:"):
            if not task:
                await query.edit_message_text("No selected task.")
                return
            name = self.storage.delete_message_permanently(task, data.split(":", 1)[1])
            await query.message.reply_text(f"Message {name} permanently deleted from VPS and future model context." if name else "Message not found.")
            await self.send_messages_manager(query.message.reply_text)
            return

        if data.startswith("updel:"):
            if not task:
                await query.edit_message_text("No selected task.")
                return
            name = self.storage.exclude_upload_metadata_from_context(task, data.split(":", 1)[1])
            await query.message.reply_text(f"Upload metadata {name} deleted from future model context." if name else "Upload metadata not found.")
            await self.send_uploads_manager(query.message.reply_text)
            return

        if data.startswith("skilldel:"):
            if not task:
                await query.edit_message_text("No selected task.")
                return
            name = self.storage.delete_custom_skill(task, data.split(":", 1)[1])
            await query.message.reply_text(f"Deleted skill {name}." if name else "Skill not found.")
            await self.send_skills_manager(query.message.reply_text)
            return

        if data.startswith("edit:"):
            if not task:
                await query.edit_message_text("No selected task.")
                return
            which = data.split(":", 1)[1]
            if which == "goal":
                context.user_data["edit_goal_task_id"] = task.task_id
                await query.edit_message_text("Send the new full goal text. Previous goal will be backed up. Use /cancelnew to cancel.")
            elif which == "constitution":
                context.user_data["edit_constitution_task_id"] = task.task_id
                await query.edit_message_text("Send the new full constitution text. Previous constitution will be backed up. Use /cancelnew to cancel.")
            return

        if not data.startswith("action:"):
            return
        action = data.split(":", 1)[1]

        if action == "newtask":
            await self.begin_newtask_wizard(context)
            await query.edit_message_text("New task. First send the task name.")
            return
        if action == "tasks":
            await query.edit_message_text("Opening task list...")
            await self.send_tasks_list()
            return
        if action == "help":
            await query.edit_message_text(self.help_text(), reply_markup=self.menu_markup(task))
            return
        if action == "menu":
            await query.edit_message_text(self.start_text(), reply_markup=self.menu_markup(task))
            return
        if action == "manage":
            await query.edit_message_text("Management menu:", reply_markup=self.manage_markup())
            return
        if action == "models":
            await query.edit_message_text(self.models_text(), reply_markup=self.models_markup())
            return
        if action == "reasoning":
            await query.edit_message_text(self.reasoning_text(), reply_markup=self.reasoning_markup())
            return
        if action == "deletetasks":
            await query.edit_message_text("Opening task list. Tap 🗑 beside a task to delete it.")
            await self.send_tasks_list()
            return

        if not task:
            await query.edit_message_text("No selected task. Use /newtask first.")
            return
        if action == "continue":
            await query.edit_message_text("Continuing selected task...")
            await self.worker.continue_after_limit(task)
        elif action == "resume":
            await query.edit_message_text("Starting/resuming selected task...")
            await self.worker.start(task)
        elif action == "pause":
            await query.edit_message_text("Pause requested...")
            await self.worker.pause(task)
        elif action == "stop":
            await query.edit_message_text("Stop requested...")
            await self.worker.stop(task)
        elif action == "status":
            await query.edit_message_text(self.status_text(task), reply_markup=self.menu_markup(task))
        elif action == "folder":
            await query.edit_message_text(f"{task.root}\nWorkspace: {task.root}/workspace\nSize: {self.storage.folder_size_human(task)}", reply_markup=self.menu_markup(task))
        elif action == "exportdata":
            path = self.storage.create_data_export_zip(task)
            await query.message.reply_text("Created core-data export ZIP. It excludes Telegram messages, logs, context snapshots, and workspace/sandbox.")
            await self.send_file(str(path), f"Core data export for {task.name}")
        elif action == "credentials":
            await query.edit_message_text(self.credentials_text(task), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Export core data ZIP", callback_data="action:exportdata"), InlineKeyboardButton("Menu", callback_data="action:menu")]]))
        elif action == "goal":
            p = self.storage.task_paths(task)["goal"]
            text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else "(missing goal)"
            await query.edit_message_text(f"Current goal:\n\n{text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Edit goal", callback_data="edit:goal"), InlineKeyboardButton("Menu", callback_data="action:menu")]]))
        elif action == "constitution":
            p = self.storage.task_paths(task)["constitution"]
            await query.message.reply_text("Sending constitution file...")
            await self.send_file(str(p), f"Current constitution for {task.name}")
            await query.message.reply_text("Use the button to replace it.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Edit constitution", callback_data="edit:constitution"), InlineKeyboardButton("Menu", callback_data="action:menu")]]))
        elif action == "messages":
            await self.send_messages_manager(query.message.reply_text)
        elif action == "uploads":
            await self.send_uploads_manager(query.message.reply_text)
        elif action == "skills":
            await self.send_skills_manager(query.message.reply_text)
        elif action == "contextmessages":
            await query.edit_message_text(self.contextmessages_text(), reply_markup=self.contextmessages_markup())

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Telegram handler error", exc_info=context.error)
        try:
            await self.send_text(f"Internal bot error: {context.error}", None)
        except TelegramError:
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.load()
    bot = AutoresearchBot(settings)
    app = bot.build()
    logger.info("Starting autoresearch with data root %s", settings.data_root)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
