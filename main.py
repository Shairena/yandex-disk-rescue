"""Compact Russian Windows desktop UI; network work stays off the UI thread."""
import argparse
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser
from api import DiskAPI
from engine import Store, Engine
import vault

APP_ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


class App(tk.Tk):
    def __init__(self, state):
        super().__init__()
        self.title('Яндекс Диск — резервная копия')
        self.geometry('1040x740')
        self.minsize(840, 620)
        self.store = Store(state)
        self.worker = self.engine = None
        self.messages = queue.Queue(maxsize=30)
        self.dest = tk.StringVar(value=self.store.setting('destination') or r'I:\YandexBackup\Disk')
        self.token = tk.StringVar()
        self.remember = tk.BooleanVar()
        self.interval = tk.StringVar(value='3')
        self.retries = tk.StringVar(value='3')
        self.backoff = tk.StringVar(value='5')
        self.status = tk.StringVar(value='Для работы нужен OAuth-токен с правом чтения всего Диска.')
        self.stats = tk.StringVar()
        self.view = tk.StringVar(value='Ожидают')
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
        opts = ttk.Frame(outer); opts.pack(fill='x', pady=8)
        for label, var, width in [('Интервал, с:', self.interval, 6), ('Попыток за цикл:', self.retries, 4), ('База ожидания, с:', self.backoff, 6)]:
            ttk.Label(opts, text=label).pack(side='left', padx=(0, 5))
            ttk.Entry(opts, textvariable=var, width=width).pack(side='left', padx=(0, 14))
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
        self.after(300, self.tick)

    def active(self):
        return self.worker and self.worker.is_alive()

    def pick_destination(self):
        value = filedialog.askdirectory(initialdir=self.dest.get())
        if value:
            self.dest.set(value)

    def set_roots(self, text):
        if not self.active():
            self.roots.delete('1.0', 'end'); self.roots.insert('1.0', text)

    def auth_help(self):
        messagebox.showinfo('OAuth — вход выполняете вы', 'Создайте своё приложение Яндекс OAuth «Для доступа к API или отладки». Выберите только «Чтение всего Диска» и «Доступ к информации о Диске». Получите токен для своего ClientID через официальный OAuth.\n\nВставьте токен в это окно, не в чат. Пароль и cookies не нужны. Сохранение — только Windows DPAPI для текущего пользователя. Ошибка 410 может зависеть от приложения/сервиса; работоспособность ещё нужно проверить.\n\nОткроется официальная инструкция.')
        webbrowser.open('https://www.yandex.com/dev/id/doc/ru/register-api')

    def forget(self):
        if self.active():
            return
        self.token.set(''); self.remember.set(False)
        (self.store.directory / 'oauth.dpapi').unlink(missing_ok=True)

    def report(self, text):
        try:
            self.messages.put_nowait(text)
        except queue.Full:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                pass
            self.messages.put_nowait(text)

    def start(self, scan_only=False):
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
        except Exception as exc:
            messagebox.showerror('Не удалось начать', str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'Локальная ошибка настройки.')

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
                messagebox.showinfo('Экспорт', 'Все шесть таблиц сохранены в новом каталоге:\n' + str(exported))
            except Exception:
                messagebox.showerror('Экспорт', 'Не удалось записать таблицы. Проверьте права и свободное место.')

    def refresh_table(self):
        view = {'Ожидают': 'pending', 'Сохранены': 'downloaded', 'Проблемы': 'failed', 'Вся опись': 'all'}
        rows = self.store.rows(view[self.view.get()], limit=500) if self.view.get() in view else self.store.folders()
        self.table.delete(*self.table.get_children())
        for row in rows[:500]:
            self.table.insert('', 'end', values=(row['path'], row['status'], row.get('size', ''), row.get('reason') or row.get('error') or ''))

    def tick(self):
        updated = False
        while True:
            try:
                self.status.set(self.messages.get_nowait()); updated = True
            except queue.Empty:
                break
        enabled = not self.active()
        for widget in (self.scan_button, self.retry_button, self.rescan_button, self.dest_button, self.dest_entry, self.token_entry):
            widget.configure(state='normal' if enabled else 'disabled')
        self.roots.configure(state='normal' if enabled else 'disabled')
        try:
            count, total_bytes, done, done_bytes = self.store.summary()
            self.stats.set(f'Проверено файлов: {done} / {count}    Байт: {done_bytes:,} / {total_bytes:,}    Не решено: {count - done}')
            folders = self.store.folders(); incomplete = sum(f['status'] != 'complete' for f in folders)
            self.summary.set(f'Не завершено папок: {incomplete} / {len(folders)}. ' + ('Полнота описи НЕ подтверждена.' if incomplete or not folders else 'Все известные папки перечислены; облако могло измениться во время обхода.'))
            self.progress.configure(maximum=max(total_bytes, 1), value=done_bytes)
            if updated or self.dirty:
                self.refresh_table(); self.dirty = False
        except Exception:
            self.status.set('Не удалось прочитать очередь. Проверьте доступ к каталогу состояния.')
        self.after(1000, self.tick)

    def close(self):
        if self.active():
            self.stop()
            self.after(200, self.close)
        else:
            self.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=APP_ROOT / 'state', help='Separate durable queue directory')
    parser.add_argument('--ui-smoke', action='store_true', help='Open empty UI briefly, without cloud access')
    args = parser.parse_args()
    app = App(args.state)
    if args.ui_smoke:
        app.after(2000, app.destroy)
    app.mainloop()


if __name__ == '__main__':
    main()
