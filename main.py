"""Compact Russian Windows desktop UI; network work stays off the UI thread."""
import argparse
from pathlib import Path
import queue
import math
import json
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser
from api import DiskAPI
from engine import Store, Engine, safe_local
from agent_control import Channel, ControlError, validate_profile
import vault

APP_ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


class App(tk.Tk):
    def __init__(self, state, channel=None):
        super().__init__()
        self.title('Яндекс Диск — резервная копия')
        self.geometry('1040x740')
        self.minsize(840, 620)
        self.store = Store(state)
        self.channel = channel or Channel(self.store)
        self.agent_session = self.channel.open_session()
        self.agent_enabled = tk.BooleanVar(value=False)
        self.closing = False
        self.worker = self.engine = None
        self.messages = queue.Queue(maxsize=30)
        self.dest = tk.StringVar(value=self.store.setting('destination') or r'I:\YandexBackup\Disk')
        self.token = tk.StringVar()
        self.remember = tk.BooleanVar()
        self.interval = tk.StringVar(value=self.saved_number('interval', '3', 0, 3600))
        self.retries = tk.StringVar(value=self.saved_number('retries', '3', 1, 20, integer=True))
        self.backoff = tk.StringVar(value=self.saved_number('backoff', '5', 1, 3600))
        self.status = tk.StringVar(value='Для работы нужен OAuth-токен с правом чтения всего Диска.')
        self.stats = tk.StringVar()
        views = ('Ожидают', 'Сохранены', 'Проблемы', 'Вся опись', 'Папки / ошибки описи')
        saved_view = self.store.setting('ui_view')
        self.view = tk.StringVar(value=saved_view if saved_view in views else views[0])
        self.summary = tk.StringVar()
        self.folder_rows = []
        self.dirty = True
        outer = ttk.Frame(self, padding=12); outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='Резервная копия Яндекс Диска', font=('Segoe UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='Только чтение облака. Ошибка или исчерпание повторов не означает потерю файла.').pack(anchor='w', pady=(2, 10))
        destrow = ttk.Frame(outer); destrow.pack(fill='x')
        ttk.Label(destrow, text='Сохранить в:').pack(side='left')
        self.dest_entry = ttk.Entry(destrow, textvariable=self.dest); self.dest_entry.pack(side='left', fill='x', expand=True, padx=8)
        self.dest_button = ttk.Button(destrow, text='Выбрать…', command=self.pick_destination); self.dest_button.pack(side='left')
        authrow = ttk.Frame(outer); authrow.pack(fill='x', pady=8)
        ttk.Label(authrow, text='OAuth-токен:').pack(side='left')
        self.token_entry = ttk.Entry(authrow, textvariable=self.token, show='•'); self.token_entry.pack(side='left', fill='x', expand=True, padx=8)
        self.paste_button = ttk.Button(authrow, text='Вставить', command=self.paste_token)
        self.paste_button.pack(side='left', padx=(0, 8))
        ttk.Checkbutton(authrow, text='Сохранить через Windows', variable=self.remember).pack(side='left')
        ttk.Button(authrow, text='Как получить', command=self.auth_help).pack(side='left', padx=(8, 0))
        ttk.Button(authrow, text='Забыть', command=self.forget).pack(side='left', padx=(8, 0))
        try:
            stored = vault.load(self.store.directory / 'oauth.dpapi')
            self.token.set(stored); self.remember.set(bool(stored))
        except Exception:
            self.status.set('Сохранённый токен не прочитан. Введите его заново.')
        rootsbox = ttk.LabelFrame(outer, text='Область копирования — пути disk:/, по одному в строке', padding=6)
        rootsbox.pack(fill='x')
        self.roots = tk.Text(rootsbox, height=3, wrap='none', font=('Segoe UI', 10))
        self.roots.pack(fill='x')
        self.roots.insert('1.0', self.store.setting('roots') or 'disk:/')
        presets = ttk.Frame(rootsbox); presets.pack(fill='x', pady=(4, 0))
        ttk.Button(presets, text='Весь Диск', command=lambda: self.set_roots('disk:/')).pack(side='left')
        ttk.Button(presets, text='Фото, загрузки, фотокамера', command=lambda: self.set_roots('disk:/Старые фото и видео\ndisk:/Загрузки\ndisk:/Фотокамера')).pack(side='left', padx=6)
        ttk.Label(presets, text='Можно указать любую папку или файл.').pack(side='left')
        self.agent_check = ttk.Checkbutton(outer, text='Разрешить и запомнить управление MCP этим профилем после перезапуска',
                                          variable=self.agent_enabled, command=self.toggle_agent)
        self.agent_check.pack(anchor='w', pady=(6, 0))
        opts = ttk.Frame(outer); opts.pack(fill='x', pady=8)
        self.option_entries = []
        for label, var, width in [('Интервал, с:', self.interval, 6), ('Попыток за цикл:', self.retries, 4), ('База ожидания, с:', self.backoff, 6)]:
            ttk.Label(opts, text=label).pack(side='left', padx=(0, 5))
            entry = ttk.Entry(opts, textvariable=var, width=width)
            entry.pack(side='left', padx=(0, 14))
            self.option_entries.append(entry)
        ttk.Label(opts, text='Один файл одновременно').pack(side='right')
        controls = ttk.Frame(outer); controls.pack(fill='x')
        self.start_button = ttk.Button(controls, text='Начать / продолжить', command=self.start); self.start_button.pack(side='left')
        self.scan_button = ttk.Button(controls, text='Только опись', command=lambda: self.start(True)); self.scan_button.pack(side='left', padx=5)
        self.pause_button = ttk.Button(controls, text='Пауза', command=self.pause); self.pause_button.pack(side='left')
        ttk.Button(controls, text='Стоп', command=self.stop).pack(side='left', padx=5)
        self.retry_button = ttk.Button(controls, text='Повторить ошибки', command=self.retry); self.retry_button.pack(side='left')
        self.rescan_button = ttk.Button(controls, text='Пересканировать', command=self.rescan); self.rescan_button.pack(side='left', padx=5)
        ttk.Button(controls, text='Экспорт CSV…', command=self.export).pack(side='right')
        ttk.Label(outer, textvariable=self.stats, font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(10, 2))
        ttk.Label(outer, textvariable=self.summary, foreground='#8a4400').pack(anchor='w')
        self.progress = ttk.Progressbar(outer, mode='determinate'); self.progress.pack(fill='x', pady=5)
        ttk.Label(outer, textvariable=self.status, wraplength=970).pack(fill='x', pady=(0, 6))
        filterrow = ttk.Frame(outer); filterrow.pack(fill='x')
        picker = ttk.Combobox(filterrow, state='readonly', textvariable=self.view, values=['Ожидают', 'Сохранены', 'Проблемы', 'Вся опись', 'Папки / ошибки описи'], width=27)
        picker.pack(side='left'); picker.bind('<<ComboboxSelected>>', lambda _: self.refresh_table())
        ttk.Label(filterrow, text='Показаны первые 500 строк; CSV содержит все строки.').pack(side='left', padx=10)
        tableframe = ttk.Frame(outer); tableframe.pack(fill='both', expand=True, pady=5)
        self.table = ttk.Treeview(tableframe, columns=('path', 'status', 'size', 'reason'), show='headings')
        for name, title, width in [('path', 'Облачный путь', 450), ('status', 'Статус', 130), ('size', 'Байт', 100), ('reason', 'Причина', 220)]:
            self.table.heading(name, text=title); self.table.column(name, width=width, minwidth=60)
        scroll = ttk.Scrollbar(tableframe, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side='left', fill='both', expand=True); scroll.pack(side='right', fill='y')
        ttk.Label(outer, text='Состояние очереди: ' + str(self.store.directory), wraplength=980).pack(anchor='w')
        ttk.Label(outer, text='Активные вложения (.lnk, .exe и другие) исключаются и остаются в отчёте. Частичные файлы сохраняются отдельно.').pack(anchor='w')
        self.protocol('WM_DELETE_WINDOW', self.close)
        for variable in (self.interval, self.retries, self.backoff, self.view):
            variable.trace_add('write', self.save_preferences)
        self.restore_agent_permission()
        self.after(300, self.tick)

    def restore_agent_permission(self):
        saved = self.store.setting('ui_mcp_consent')
        if not saved:
            return
        try:
            consent = json.loads(saved)
            if not isinstance(consent, dict) or set(consent) != {'profile', 'account'}:
                raise ValueError('invalid_saved_consent')
            profile = validate_profile(consent['profile'])
            if (profile != self.profile() or consent['account'] != self.store.setting('account')
                    or not self.token.get()):
                self.status.set('MCP не восстановлен: профиль, аккаунт или сохранённый токен изменились.')
                return
            self.channel.approve(self.agent_session, profile)
            self.agent_enabled.set(True)
            self.status.set('Сохранённое разрешение MCP восстановлено. Скачивание ещё не запущено.')
        except Exception:
            self.agent_enabled.set(False)
            self.channel.approve(self.agent_session)
            self.status.set('Не удалось восстановить разрешение MCP. Включите его заново в окне.')

    @staticmethod
    def valid_number(value, low, high, integer=False):
        try:
            number = int(value) if integer else float(value)
            return math.isfinite(number) and low <= number <= high
        except (ValueError, TypeError, OverflowError):
            return False

    def saved_number(self, name, default, low, high, integer=False):
        value = self.store.setting('ui_' + name)
        return value if self.valid_number(value, low, high, integer) else default

    def save_preferences(self, *_):
        try:
            for name, variable, low, high, integer in (
                    ('interval', self.interval, 0, 3600, False),
                    ('retries', self.retries, 1, 20, True),
                    ('backoff', self.backoff, 1, 3600, False)):
                value = variable.get()
                if self.valid_number(value, low, high, integer):
                    self.store.setting('ui_' + name, value)
            self.store.setting('ui_view', self.view.get())
        except Exception:
            self.status.set('Не удалось сохранить настройки. Проверьте доступ к каталогу состояния.')

    def profile(self):
        from engine import cloud_path
        return validate_profile({'destination': self.dest.get().strip(),
                                 'roots': [cloud_path(p) for p in self.roots.get('1.0', 'end').splitlines() if p.strip()],
                                 'interval': float(self.interval.get()), 'retries': int(self.retries.get()), 'backoff': float(self.backoff.get())})

    def toggle_agent(self):
        try:
            if self.closing or self.active() and self.agent_enabled.get():
                raise ControlError('Сначала остановите текущую операцию.')
            self.channel.approve(self.agent_session, self.profile() if self.agent_enabled.get() else None, remember=True)
            self.status.set('Разрешение MCP сохранено для текущего профиля.' if self.agent_enabled.get() else 'MCP отключён; сохранённое разрешение удалено.')
        except (ValueError, OSError):
            self.agent_enabled.set(False)
            self.channel.approve(self.agent_session, remember=True)
            messagebox.showerror('MCP', 'Не удалось разрешить MCP. Остановите работу и проверьте пути/параметры.')

    def phase(self):
        if self.closing:
            return 'stopping' if self.active() else 'closed'
        if not self.active():
            return 'idle'
        if self.engine.stop.is_set():
            return 'stopping'
        if self.engine.paused.is_set():
            return 'paused'
        return 'pause_requested' if self.engine.pause.is_set() else 'running'

    def handle_agent(self, command, profile):
        if self.closing or self.profile() != profile:
            raise ControlError('approved_profile_changed')
        if command in ('start', 'scan'):
            if self.active():
                return {'outcome': 'already_active'}
            return {'outcome': 'started' if self.start(command == 'scan', agent=True) else 'not_started_check_gui'}
        if command == 'pause':
            if self.active():
                self.engine.pause.set()
            return {'outcome': 'pause_requested' if self.active() else 'already_idle'}
        if command == 'resume':
            if self.active():
                self.engine.pause.clear()
            return {'outcome': 'resume_requested' if self.active() else 'idle_use_start'}
        if command == 'stop':
            self.stop()
            return {'outcome': 'stop_requested' if self.active() else 'already_idle'}
        if self.active():
            return {'outcome': 'busy_stop_or_wait_first'}
        if command == 'retry':
            self.store.retry(profile['roots'])
        elif command == 'rescan':
            self.store.rescan(profile['roots'])
        elif command == 'export':
            exported = self.store.export(safe_local(self.store.directory, 'agent-exports'), roots=profile['roots'])
            return {'outcome': 'exported', 'directory': str(exported)}
        self.dirty = True
        return {'outcome': 'applied_no_download_started'}

    def active(self):
        return self.worker and self.worker.is_alive()

    def pick_destination(self):
        value = filedialog.askdirectory(initialdir=self.dest.get())
        if value:
            self.dest.set(value)

    def set_roots(self, text):
        if not self.active() and not self.agent_enabled.get():
            self.roots.delete('1.0', 'end'); self.roots.insert('1.0', text)

    def auth_help(self):
        messagebox.showinfo('OAuth — вход выполняете вы', 'Создайте своё приложение Яндекс OAuth «Для доступа к API или отладки». Выберите только «Чтение всего Диска» и «Доступ к информации о Диске». Получите токен для своего ClientID через официальный OAuth.\n\nВставьте токен в это окно, не в чат. Пароль и cookies не нужны. Сохранение — только Windows DPAPI для текущего пользователя. Ошибка 410 может зависеть от приложения/сервиса; работоспособность ещё нужно проверить.\n\nОткроется официальная инструкция.')
        webbrowser.open('https://www.yandex.com/dev/id/doc/ru/register-api')

    def forget(self):
        if self.active():
            return
        self.channel.approve(self.agent_session, remember=True)
        self.agent_enabled.set(False)
        self.token.set(''); self.remember.set(False)
        (self.store.directory / 'oauth.dpapi').unlink(missing_ok=True)

    def paste_token(self):
        if self.active():
            return
        self.token_entry.focus_set()
        self.token_entry.selection_range(0, 'end')
        # The widget handles the clipboard locally; never log or persist its value.
        self.token_entry.event_generate('<<Paste>>')

    def report(self, text):
        try:
            self.messages.put_nowait(text)
        except queue.Full:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                pass
            self.messages.put_nowait(text)

    def start(self, scan_only=False, agent=False):
        if self.active():
            if self.engine.pause.is_set():
                self.engine.pause.clear(); self.pause_button.configure(text='Пауза')
            return
        try:
            from engine import cloud_path
            roots = [cloud_path(p) for p in self.roots.get('1.0', 'end').splitlines() if p.strip()]
            if not roots:
                raise ValueError('Выберите хотя бы один облачный путь.')
            api = DiskAPI(self.token.get())
            engine = Engine(self.store, api, self.interval.get(), self.retries.get(), self.backoff.get(), self.report)
            if self.remember.get():
                vault.save(self.store.directory / 'oauth.dpapi', self.token.get())
            else:
                (self.store.directory / 'oauth.dpapi').unlink(missing_ok=True)
            self.store.setting('roots', '\n'.join(roots))
            self.engine = engine
            destination = self.dest.get().strip()
            if not destination or not Path(destination).is_absolute():
                raise ValueError('Нужен абсолютный путь назначения.')
            self.worker = threading.Thread(target=engine.run, args=(destination, roots, scan_only), daemon=False)
            self.worker.start()
            self.status.set('Проверка аккаунта…')
            return True
        except Exception as exc:
            if agent:
                self.status.set('MCP: запуск не выполнен; проверьте токен и настройки в окне.')
            else:
                messagebox.showerror('Не удалось начать', str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'Локальная ошибка настройки.')
            return False

    def pause(self):
        if self.active():
            if self.engine.pause.is_set():
                self.engine.pause.clear(); self.pause_button.configure(text='Пауза')
            else:
                self.engine.pause.set(); self.pause_button.configure(text='Продолжить')
                self.status.set('Пауза запрошена. Текущий сетевой запрос завершится или истечёт по таймауту.')

    def stop(self):
        if self.active():
            self.engine.stop.set(); self.engine.pause.clear()
            self.status.set('Останавливаемся; ожидание сетевого запроса — до 30 секунд.')

    def retry(self):
        if not self.active():
            self.store.retry(); self.refresh_table()
            self.status.set('Ошибки поставлены в новый цикл. История попыток сохранена. Нажмите «Начать».')

    def rescan(self):
        if not self.active():
            self.store.rescan(); self.refresh_table()
            self.status.set('Опись будет пройдена заново. История и локальные файлы сохраняются.')

    def export(self):
        directory = filedialog.askdirectory(title='Каталог для таблиц CSV')
        if directory:
            try:
                exported = self.store.export(directory)
                messagebox.showinfo('Экспорт', 'Все таблицы, включая неактуальные и непроверенные записи, сохранены в новом каталоге:\n' + str(exported))
            except Exception:
                messagebox.showerror('Экспорт', 'Не удалось записать таблицы. Проверьте права и свободное место.')

    def refresh_table(self):
        view = {'Ожидают': 'pending', 'Сохранены': 'downloaded', 'Проблемы': 'failed', 'Вся опись': 'all'}
        rows = self.store.rows(view[self.view.get()], limit=500) if self.view.get() in view else self.store.folders()
        self.table.delete(*self.table.get_children())
        for row in rows[:500]:
            label = row['status'] if row['presence'] == 'observed' else row['presence'] + ' / ' + row['status']
            self.table.insert('', 'end', values=(row['path'], label, row.get('size', ''), row.get('reason') or row.get('error') or ''))

    def tick(self):
        try:
            self.channel.publish(self.agent_session, self.phase(), bool(self.token.get()))
            if self.agent_enabled.get() and not self.closing:
                self.channel.dispatch_one(self.agent_session, self.handle_agent)
        except (ControlError, OSError):
            pass
        updated = False
        while True:
            try:
                self.status.set(self.messages.get_nowait()); updated = True
            except queue.Empty:
                break
        enabled = not self.active()
        for widget in (self.scan_button, self.retry_button, self.rescan_button, self.token_entry, self.paste_button):
            widget.configure(state='normal' if enabled else 'disabled')
        scope_enabled = enabled and not self.agent_enabled.get()
        self.dest_button.configure(state='normal' if scope_enabled else 'disabled')
        self.dest_entry.configure(state='normal' if scope_enabled else 'disabled')
        self.roots.configure(state='normal' if scope_enabled else 'disabled')
        for entry in self.option_entries:
            entry.configure(state='normal' if scope_enabled else 'disabled')
        self.agent_check.configure(state='disabled' if self.closing else 'normal')
        try:
            count, total_bytes, done, done_bytes = self.store.summary()
            self.stats.set(f'Проверено файлов: {done} / {count}    Байт: {done_bytes:,} / {total_bytes:,}    Не решено: {count - done}')
            folders = self.store.folders()
            historical = sum(f['presence'] == 'not_current' for f in folders)
            unknown = sum(f['presence'] == 'unverified' for f in folders)
            incomplete = sum(f['presence'] != 'not_current' and (f['status'] != 'complete' or f['presence'] == 'unverified') for f in folders)
            self.summary.set(f'Не завершено папок: {incomplete}. Не проверены: {unknown}; неактуальны: {historical}. ' + ('Полнота описи НЕ подтверждена.' if incomplete or not folders else 'Известный обход завершён; история и локальные копии сохранены.'))
            self.progress.configure(maximum=max(total_bytes, 1), value=done_bytes)
            if updated or self.dirty:
                self.refresh_table(); self.dirty = False
        except Exception:
            self.status.set('Не удалось прочитать очередь. Проверьте доступ к каталогу состояния.')
        self.after(1000, self.tick)

    def close(self):
        if not self.closing:
            self.closing = True
            self.agent_enabled.set(False)
            self.channel.approve(self.agent_session)
        if self.active():
            self.stop()
            self.after(200, self.close)
        else:
            self.channel.publish(self.agent_session, 'closed', False)
            self.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, help='Separate durable queue directory; mandatory for MCP')
    parser.add_argument('--mcp', action='store_true', help='Run only stdio agent control; never starts a download worker')
    parser.add_argument('--ui-smoke', action='store_true', help='Open empty UI briefly, without cloud access')
    args = parser.parse_args()
    if args.mcp:
        if not args.state:
            parser.error('--mcp requires explicit --state')
        from mcp_server import serve
        serve(Store(args.state))
        return
    state = args.state or APP_ROOT / 'state'
    channel = Channel(Store(state))
    try:
        with channel.gui_lease():
            app = App(state, channel)
            if args.ui_smoke:
                app.after(2000, app.close)
            app.mainloop()
    except ControlError:
        messagebox.showerror('Очередь занята', 'Другое окно уже управляет этой очередью.')


if __name__ == '__main__':
    main()
