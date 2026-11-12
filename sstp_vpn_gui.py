#!/usr/bin/env python3
import os
import json
import colorsys
import fcntl
import base64
import tempfile
import re
import threading
from collections import Counter
from pathlib import Path
import subprocess
import sys

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from gi.repository import Gio, Gtk, GLib, Gdk, GdkPixbuf, AyatanaAppIndicator3

APP_NAME = 'SSTP VPN'
APP_ID = 'io.github.zver.SSTPVPN'
VPN_TYPE = 'org.freedesktop.NetworkManager.sstp'

PALETTES = {
    'light': dict(bg='#fdf7ff', surface='#f3edf7', high='#ece6f0', ink='#1d1b20', muted='#625b71',
                  primary='#6750a4', onprimary='#ffffff', container='#eaddff', oncontainer='#21005d',
                  outline='#cac4d0', error='#b3261e', success='#386a20'),
    'dark': dict(bg='#141218', surface='#211f26', high='#2b2930', ink='#e6e0e9', muted='#cac4d0',
                 primary='#d0bcff', onprimary='#381e72', container='#4f378b', oncontainer='#eaddff',
                 outline='#49454f', error='#f2b8b5', success='#a4d18c'),
}


SETTINGS_PATH = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'sstp-vpn' / 'appearance.json'
LOCK_PATH = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / f'sstp-vpn-{os.getuid()}.lock'
VAULT_PATH = Path(__file__).resolve().with_name('connections.vault')


def contrast(first, second):
    def luminance(value):
        channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    a, b = sorted((luminance(first), luminance(second)))
    return (b + 0.05) / (a + 0.05)


def wallpaper_seed(path):
    image = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 64, 64, True)
    pixels, stride, channels = image.get_pixels(), image.get_rowstride(), image.get_n_channels()
    counts = Counter()
    for y in range(image.get_height()):
        for x in range(image.get_width()):
            offset = y * stride + x * channels
            if channels == 4 and pixels[offset + 3] < 128:
                continue
            rgb = tuple(pixels[offset + i] for i in range(3))
            counts[tuple(v // 24 for v in rgb)] += 1
    if not counts:
        return (0.74, 0.5)
    def score(item):
        bucket, count = item
        rgb = [min(255, v * 24 + 12) / 255 for v in bucket]
        hue, light, sat = colorsys.rgb_to_hls(*rgb)
        return count * (0.25 + sat) * (0.4 if light < 0.12 or light > 0.9 else 1)
    bucket = max(counts.items(), key=score)[0]
    hue, light, sat = colorsys.rgb_to_hls(*[min(255, v * 24 + 12) / 255 for v in bucket])
    return hue, min(0.7, max(0.25, sat))


def dynamic_palette(theme, seed):
    hue, saturation = seed
    def tone(lightness, chroma=1):
        rgb = colorsys.hls_to_rgb(hue, lightness, saturation * chroma)
        return '#' + ''.join(f'{round(c * 255):02x}' for c in rgb)
    if theme == 'dark':
        result = dict(bg=tone(.085, .18), surface=tone(.14, .18), high=tone(.20, .18),
                      ink='#faf7ff', muted='#e2dce9', primary=tone(.34), onprimary='#ffffff',
                      container=tone(.25, .7), oncontainer='#faf7ff', outline=tone(.55, .4),
                      error='#ffb4ab', success='#b3e6a0')
        for key in ('primary', 'container'):
            lightness = .34 if key == 'primary' else .25
            while contrast(result[key], '#ffffff') < 4.5 and lightness > .08:
                lightness -= .02
                result[key] = tone(lightness, 1 if key == 'primary' else .7)
    else:
        result = dict(bg=tone(.98, .4), surface=tone(.95, .35), high=tone(.91, .3),
                      ink='#211b27', muted='#504759', primary=tone(.34), onprimary='#ffffff',
                      container=tone(.88, .7), oncontainer='#251335', outline=tone(.60, .3),
                      error='#a51f23', success='#285b21')
        lightness = .34
        while contrast(result['primary'], '#ffffff') < 4.5 and lightness > .08:
            lightness -= .02
            result['primary'] = tone(lightness)
    # Check every text role against its own surface, including neutral wallpapers.
    for foreground, background in [('ink', 'high'), ('muted', 'high'), ('oncontainer', 'container'),
                                   ('success', 'high'), ('error', 'surface')]:
        if contrast(result[foreground], result[background]) < 4.5:
            result[foreground] = '#ffffff' if theme == 'dark' else '#211b27'
    return result


def vault_key(password, salt):
    derived = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000).derive(password.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(derived))



def material_css(p):
    return ("""
    window { background: %(bg)s; color: %(ink)s; font-family: 'Noto Sans', sans-serif; font-size: 14px; }
    messagedialog, messagedialog box, messagedialog label, dialog, dialog box, dialog label {
        background-color: %(surface)s; color: %(ink)s; opacity: 1; }
    messagedialog button { background: %(primary)s; color: %(onprimary)s; }
    label, checkbutton, checkbutton label, image { color: %(ink)s; }
    button label, button image { color: inherit; }
    headerbar label, headerbar button image { color: %(ink)s; }
    viewport { background: transparent; }
    tooltip, tooltip label { background: %(surface)s; color: %(ink)s; }
    checkbutton check { background: %(high)s; color: %(ink)s; border: 1px solid %(outline)s; border-radius: 4px; }
    checkbutton check:checked { background: %(primary)s; color: %(onprimary)s; }
    entry:disabled { color: %(muted)s; }
    entry placeholder { color: %(muted)s; }
    .scrim { background: %(bg)s; border-radius: 20px; padding: 16px; }
    headerbar { background: %(bg)s; color: %(ink)s; border: none; box-shadow: none; padding: 8px 16px; }
    headerbar .title { font-size: 15px; font-weight: 600; }
    button { background: %(surface)s; background-image: none; color: %(ink)s; border: none;
             box-shadow: none; text-shadow: none; border-radius: 24px; padding: 11px 20px; min-height: 20px; }
    button:hover { background: %(high)s; }
    button:focus { outline: 2px solid %(primary)s; outline-offset: -3px; }
    button.primary { background: %(primary)s; color: %(onprimary)s; font-weight: 600; }
    button.tonal { background: %(container)s; color: %(oncontainer)s; font-weight: 600; }
    button.danger { color: %(error)s; }
    button.flat { background: %(surface)s; color: %(ink)s; }
    button:disabled, button:disabled label { background: %(high)s; color: %(muted)s; opacity: 1; }
    .display { font-size: 32px; font-weight: 500; }
    .headline { font-size: 24px; font-weight: 500; }
    .title { font-size: 17px; font-weight: 600; }
    .muted { color: %(muted)s; }
    .eyebrow { color: %(ink)s; font-size: 11px; font-weight: bold; letter-spacing: 2px; }
    .hero { background: %(container)s; color: %(oncontainer)s; border-radius: 28px; padding: 24px; }
    .hero label, .hero .muted { color: %(oncontainer)s; }
    .panel { background: %(surface)s; border-radius: 24px; padding: 24px; }
    notebook, notebook stack { background: transparent; border: none; }
    notebook header { background: %(surface)s; border: none; border-radius: 24px; padding: 5px; margin-bottom: 24px; }
    notebook header tabs { border: none; }
    notebook tab { background: transparent; color: %(muted)s; border: none; border-radius: 20px; padding: 12px 24px; }
    notebook tab:checked { background: %(container)s; color: %(oncontainer)s; box-shadow: none; }
    notebook tab label { color: %(muted)s; font-weight: 600; }
    notebook tab:checked label { color: %(oncontainer)s; }
    list { background: transparent; border: none; }
    row { background: %(surface)s; color: %(ink)s; border: 2px solid transparent; border-radius: 20px; margin: 0 0 10px; padding: 16px; }
    row:hover { background: %(high)s; }
    row:selected { background: %(container)s; color: %(oncontainer)s; border-color: %(primary)s; }
    row:selected label, row:selected image, row:selected .muted { color: %(oncontainer)s; }
    .badge { background: %(high)s; color: %(muted)s; border-radius: 14px; padding: 7px 12px; font-size: 12px; }
    row:selected .badge { background: %(high)s; color: %(muted)s; }
    row:selected .badge.connected, .connected { color: %(success)s; }
    entry { background: %(high)s; color: %(ink)s; border: 1px solid transparent; background-image: none;
            border-radius: 12px; padding: 13px 16px; min-height: 22px; box-shadow: none; caret-color: %(primary)s; }
    entry:focus { border-color: %(primary)s; }
    entry selection { background: %(container)s; color: %(oncontainer)s; }
    .field-label { color: %(muted)s; font-size: 12px; font-weight: 600; }
    separator { background: %(outline)s; min-height: 1px; }
    scrolledwindow { border: none; }
    menu { background: %(surface)s; color: %(ink)s; padding: 8px; border-radius: 12px; }
    menuitem { padding: 10px 16px; border-radius: 8px; }
    menuitem label { color: %(ink)s; }
    menuitem:hover label { color: %(oncontainer)s; }
    menuitem:hover { background: %(container)s; color: %(oncontainer)s; }
    """ % p).encode()



def run_nmcli(*args):
    return subprocess.run(['nmcli', *args], text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)


def profiles():
    result = run_nmcli('-t', '-f', 'NAME,UUID,TYPE', 'connection', 'show')
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    found = []
    for line in result.stdout.splitlines():
        try:
            name, uuid, kind = line.rsplit(':', 2)
        except ValueError:
            continue
        if kind != 'vpn':
            continue
        service = run_nmcli('-g', 'vpn.service-type', 'connection', 'show', 'uuid', uuid)
        if service.returncode == 0 and service.stdout.strip() == VPN_TYPE:
            data = run_nmcli('-g', 'vpn.data', 'connection', 'show', 'uuid', uuid).stdout.strip()
            values = {key.strip(): value.strip() for part in re.split(r'[,\n]+', data) if '=' in part for key, value in [part.split('=', 1)]}
            active = run_nmcli('-g', 'GENERAL.STATE', 'connection', 'show', 'uuid', uuid)
            found.append({'name': name, 'uuid': uuid, 'gateway': values.get('gateway', ''),
                          'user': values.get('user', ''), 'active': active.stdout.strip() == 'activated'})
    return sorted(found, key=lambda item: item['name'].lower())


class VPNApp:
    def __init__(self):
        self.current_uuid = None
        self.items = []
        self.vault = None
        self.vault_cipher = None
        self.window = Gtk.Window(title=APP_NAME)
        self.window.set_default_size(860, 820)
        self.window.set_size_request(700, 660)
        self.theme = 'dark'
        self.wallpaper_path = ''
        self.wallpaper_pixbuf = None
        self.wallpaper_cache = None
        self.seed = (.74, .5)
        try:
            settings = json.loads(SETTINGS_PATH.read_text())
            self.theme = settings.get('theme', 'dark')
            if self.theme not in PALETTES:
                self.theme = 'dark'
            self.wallpaper_path = settings.get('wallpaper', '')
            if self.wallpaper_path:
                self.load_wallpaper(self.wallpaper_path)
        except (OSError, ValueError, TypeError, GLib.Error):
            self.wallpaper_path = ''
            self.wallpaper_pixbuf = None
        self.window.set_border_width(0)
        self.window.connect('delete-event', self.hide_window)
        self.window.connect('destroy', self.quit)
        self.apply_css()
        self.build_ui()
        self.create_indicator()
        self.refresh()

    def apply_css(self):
        if not hasattr(self, 'css_provider'):
            self.css_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_screen(
                self.window.get_screen(), self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.css_provider.load_from_data(material_css(dynamic_palette(self.theme, self.seed)))

    def toggle_theme(self, *_args):
        self.theme = 'light' if self.theme == 'dark' else 'dark'
        self.apply_css()
        self.theme_button.set_label('Светлая тема' if self.theme == 'dark' else 'Тёмная тема')
        self.background.queue_draw()
        self.save_appearance()

    def unlock_vault(self):
        if self.vault is not None:
            return True
        first_run = not VAULT_PATH.exists()
        dialog = Gtk.Dialog(title='Защита подключений', transient_for=self.window, modal=True)
        dialog.add_buttons('Отмена', Gtk.ResponseType.CANCEL, 'Продолжить', Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_border_width(18)
        content.pack_start(self.label('Создайте мастер-пароль для шифрования данных VPN.' if first_run else 'Введите мастер-пароль для расшифровки данных VPN.', 'title'), False, False, 0)
        content.pack_start(self.label('Пароль не хранится в приложении. При его утрате расшифровать сохранённые подключения невозможно.', 'muted'), False, False, 0)
        password = Gtk.Entry()
        password.set_visibility(False)
        password.set_placeholder_text('Мастер-пароль')
        content.pack_start(password, False, False, 0)
        confirm = None
        if first_run:
            confirm = Gtk.Entry()
            confirm.set_visibility(False)
            confirm.set_placeholder_text('Повторите мастер-пароль')
            content.pack_start(confirm, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        phrase = password.get_text()
        repeated = confirm.get_text() if confirm else phrase
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return False
        if len(phrase) < 8:
            self.message('Используйте мастер-пароль длиной не меньше 8 символов.', Gtk.MessageType.WARNING)
            return False
        if phrase != repeated:
            self.message('Мастер-пароли не совпадают.', Gtk.MessageType.WARNING)
            return False
        try:
            if first_run:
                salt = os.urandom(16)
                self.vault_cipher = vault_key(phrase, salt)
                self.vault = {'version': 1, 'profiles': {}}
                self.write_vault(salt)
            else:
                envelope = json.loads(VAULT_PATH.read_text())
                salt = base64.b64decode(envelope['salt'])
                self.vault_cipher = vault_key(phrase, salt)
                self.vault = json.loads(self.vault_cipher.decrypt(envelope['ciphertext'].encode()).decode())
        except (OSError, ValueError, KeyError, InvalidToken) as exc:
            self.vault = None
            self.vault_cipher = None
            self.message('Не удалось открыть зашифрованное хранилище: ' + str(exc), Gtk.MessageType.ERROR)
            return False
        return True

    def write_vault(self, salt=None):
        if salt is None:
            envelope = json.loads(VAULT_PATH.read_text())
            salt = base64.b64decode(envelope['salt'])
        encrypted = self.vault_cipher.encrypt(json.dumps(self.vault, ensure_ascii=False, separators=(',', ':')).encode()).decode()
        temporary = VAULT_PATH.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': 1, 'salt': base64.b64encode(salt).decode(), 'ciphertext': encrypted}))
        os.chmod(temporary, 0o600)
        temporary.replace(VAULT_PATH)

    def label(self, text, style=None):
        widget = Gtk.Label(label=text, xalign=0)
        widget.set_line_wrap(True)
        if style:
            widget.get_style_context().add_class(style)
        return widget

    def button(self, text, callback, style=None):
        widget = Gtk.Button(label=text)
        widget.connect('clicked', lambda _button: callback())
        if style:
            widget.get_style_context().add_class(style)
        return widget

    def build_ui(self):
        header = Gtk.HeaderBar(title=APP_NAME)
        header.set_show_close_button(True)
        self.theme_button = self.button('Светлая тема' if self.theme == 'dark' else 'Тёмная тема', self.toggle_theme, 'flat')
        header.pack_end(self.theme_button)
        self.window.set_titlebar(header)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        outer.set_border_width(28)
        overlay = Gtk.Overlay()
        self.background = Gtk.DrawingArea()
        self.background.set_hexpand(True)
        self.background.set_vexpand(True)
        self.background.connect('draw', self.draw_wallpaper)
        overlay.add(self.background)
        overlay.add_overlay(outer)
        self.window.add(overlay)
        brand = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        brand.get_style_context().add_class('scrim')
        brand.pack_start(self.label('ВАША ЛИЧНАЯ СЕТЬ', 'eyebrow'), False, False, 0)
        brand.pack_start(self.label('Связь под вашим контролем', 'display'), False, False, 0)
        brand.pack_start(self.label('Подключайтесь к своему роутеру в пару нажатий.', 'muted'), False, False, 0)
        outer.pack_start(brand, False, False, 0)

        self.notebook = Gtk.Notebook()
        self.notebook.set_show_border(False)
        outer.pack_start(self.notebook, True, True, 0)
        connections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hero.get_style_context().add_class('hero')
        self.hero_title = self.label('Готовы к подключению', 'headline')
        self.hero_detail = self.label('Выберите сохранённую сеть или создайте новую.', 'muted')
        hero.pack_start(self.hero_title, False, False, 0)
        hero.pack_start(self.hero_detail, False, False, 0)
        connections.pack_start(hero, False, False, 0)
        heading = Gtk.Box(spacing=8)
        heading.get_style_context().add_class('scrim')
        heading.pack_start(self.label('Мои подключения', 'title'), True, True, 0)
        heading.pack_end(self.button('+ Новый профиль', self.new_profile, 'tonal'), False, False, 0)
        heading.pack_end(self.button('Обновить', self.refresh, 'flat'), False, False, 0)
        connections.pack_start(heading, False, False, 0)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect('row-selected', self.on_row_selected)
        self.listbox.connect('row-activated', lambda _box, _row: self.open_editor())
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        empty.set_border_width(24)
        empty.get_style_context().add_class('scrim')
        empty.pack_start(self.label('Здесь появятся ваши сети', 'title'), False, False, 0)
        empty.pack_start(self.label('Нажмите «Новый профиль», чтобы добавить SSTP-подключение.', 'muted'), False, False, 0)
        empty.show_all()
        self.listbox.set_placeholder(empty)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.listbox)
        connections.pack_start(scroll, True, True, 0)
        actions = Gtk.Box(spacing=8)
        self.selection_buttons = []
        for text, callback, style in [('Подключить', self.connect_selected, 'primary'),
                                      ('Отключить', self.disconnect_selected, 'tonal'),
                                      ('Изменить', self.open_editor, 'flat'),
                                      ('Удалить', self.delete_selected, 'danger')]:
            button = self.button(text, callback, style)
            actions.pack_start(button, False, False, 0)
            self.selection_buttons.append(button)
        connections.pack_start(actions, False, False, 0)
        self.notebook.append_page(connections, Gtk.Label(label='Подключения'))

        editor_scroll = Gtk.ScrolledWindow()
        editor_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.editor_title = self.label('Новый профиль', 'headline')
        self.editor_title.get_style_context().add_class('scrim')
        editor.pack_start(self.editor_title, False, False, 0)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        panel.get_style_context().add_class('panel')
        form = Gtk.Grid(column_spacing=18, row_spacing=16)
        self.name = self.add_entry(form, 0, 'Название подключения')
        self.gateway = self.add_entry(form, 1, 'Сервер или DDNS-имя')
        self.username = self.add_entry(form, 2, 'Имя пользователя')
        self.password = self.add_entry(form, 3, 'Пароль', hidden=True)
        self.name.set_placeholder_text('Например, домашняя сеть')
        self.gateway.set_placeholder_text('vpn.example.com')
        self.username.set_placeholder_text('Пользователь SSTP на роутере')
        panel.pack_start(form, False, False, 0)
        show_password = Gtk.CheckButton(label='Показать пароль')
        show_password.connect('toggled', lambda widget: self.password.set_visibility(widget.get_active()))
        panel.pack_start(show_password, False, False, 0)
        editor.pack_start(panel, False, False, 0)
        actions = Gtk.Box(spacing=10)
        actions.pack_start(self.button('Сохранить изменения', self.save_profile, 'primary'), False, False, 0)
        actions.pack_start(self.button('Новый профиль', self.new_profile, 'tonal'), False, False, 0)
        editor.pack_start(actions, False, False, 0)
        editor_scroll.add(editor)
        self.notebook.append_page(editor_scroll, Gtk.Label(label='Редактор'))

        status = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        status.get_style_context().add_class('panel')
        status.pack_start(self.label('Состояние сети', 'headline'), False, False, 0)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        panel.get_style_context().add_class('panel')
        self.status_label = self.label('', 'muted')
        self.status_label.set_selectable(True)
        panel.pack_start(self.status_label, False, False, 0)
        panel.pack_start(self.button('Обновить состояние', self.refresh, 'tonal'), False, False, 0)
        status.pack_start(panel, False, False, 0)
        status.pack_start(self.label('Всегда под рукой', 'title'), False, False, 0)
        status.pack_start(self.label('Закройте окно, чтобы оставить приложение в трее. '
                                    'В меню значка можно управлять VPN и завершить приложение.', 'muted'), False, False, 0)
        self.notebook.append_page(status, Gtk.Label(label='Состояние'))
        footer = self.label('SSTP VPN  •  Закрытие окна сворачивает приложение в трей', 'muted')
        footer.get_style_context().add_class('scrim')
        outer.pack_end(footer, False, False, 0)
        appearance = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        appearance.get_style_context().add_class('panel')
        appearance.pack_start(self.label('Оформление', 'headline'), False, False, 0)
        appearance.pack_start(self.label('Выберите обои — акцентные цвета приложения подстроятся под изображение. '
                                        'Текст и карточки сохраняют контраст на любом фоне.', 'muted'), False, False, 0)
        self.wallpaper_label = self.label(self.wallpaper_path or 'Обои не выбраны', 'muted')
        self.wallpaper_label.set_max_width_chars(45)
        self.wallpaper_label.set_line_wrap(True)
        appearance.pack_start(self.wallpaper_label, False, False, 0)
        wallpaper_actions = Gtk.Box(spacing=10)
        wallpaper_actions.pack_start(self.button('Выбрать обои', self.choose_wallpaper, 'primary'), False, False, 0)
        wallpaper_actions.pack_start(self.button('Убрать обои', self.clear_wallpaper, 'tonal'), False, False, 0)
        appearance.pack_start(wallpaper_actions, False, False, 0)
        appearance.pack_start(self.label('Изображение заполняет фон с сохранением пропорций. '
                                        'Обои и тема сохраняются после перезапуска.', 'muted'), False, False, 0)
        self.notebook.append_page(appearance, Gtk.Label(label='Оформление'))

    def save_appearance(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = SETTINGS_PATH.with_suffix('.tmp')
            temporary.write_text(json.dumps({'theme': self.theme, 'wallpaper': self.wallpaper_path}, ensure_ascii=False))
            temporary.replace(SETTINGS_PATH)
        except OSError as exc:
            self.message('Не удалось сохранить оформление: ' + str(exc), Gtk.MessageType.WARNING)

    def load_wallpaper(self, path):
        # Prepare everything before replacing the current background.
        seed = wallpaper_seed(path)
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 1920, 1920, True)
        self.wallpaper_path, self.wallpaper_pixbuf = path, pixbuf
        self.seed = seed
        self.wallpaper_cache = None

    def choose_wallpaper(self):
        # Dolphin and Nautilus are file managers, not file-picker APIs: they
        # cannot return a user's selected path to another process. `yad` runs
        # as a separate, unstyled system dialog and returns that path reliably.
        chooser = ['yad', '--file', '--title=Выберите обои', '--file-filter=Изображения | *.png *.jpg *.jpeg *.webp *.bmp']
        pictures = os.path.join(os.path.expanduser('~'), 'Pictures')
        if os.path.isdir(pictures):
            chooser.append(f'--filename={pictures}/')
        threading.Thread(target=self.run_wallpaper_picker, args=(chooser,), daemon=True).start()

    def run_wallpaper_picker(self, chooser):
        result = subprocess.run(chooser, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        path = result.stdout.strip() if result.returncode == 0 else ''
        if path:
            GLib.idle_add(self.apply_wallpaper_path, path)

    def apply_wallpaper_path(self, path):
        try:
            self.load_wallpaper(path)
        except (GLib.Error, OSError, ValueError) as exc:
            self.message('Не удалось открыть изображение: ' + str(exc), Gtk.MessageType.ERROR)
            return False
        self.wallpaper_label.set_text(path)
        self.apply_css()
        self.background.queue_draw()
        self.save_appearance()
        return False

    def clear_wallpaper(self):
        self.wallpaper_path = ''
        self.wallpaper_pixbuf = None
        self.wallpaper_cache = None
        self.seed = (.74, .5)
        self.wallpaper_label.set_text('Обои не выбраны')
        self.apply_css()
        self.background.queue_draw()
        self.save_appearance()

    def draw_wallpaper(self, widget, cr):
        if self.wallpaper_pixbuf is None:
            return False
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        if width < 1 or height < 1:
            return False
        if self.wallpaper_cache is None or self.wallpaper_cache[0] != (width, height):
            image = self.wallpaper_pixbuf
            factor = max(width / image.get_width(), height / image.get_height())
            scaled = image.scale_simple(max(width, round(image.get_width() * factor)),
                                       max(height, round(image.get_height() * factor)), GdkPixbuf.InterpType.BILINEAR)
            self.wallpaper_cache = ((width, height), scaled)
        image = self.wallpaper_cache[1]
        Gdk.cairo_set_source_pixbuf(cr, image, (width - image.get_width()) / 2, (height - image.get_height()) / 2)
        cr.paint()
        if self.theme == 'dark':
            cr.set_source_rgba(.03, .02, .05, .30)
        else:
            cr.set_source_rgba(1, 1, 1, .22)
        cr.paint()
        return False

    def add_entry(self, grid, row, label, hidden=False):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.pack_start(self.label(label, 'field-label'), False, False, 0)
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        if hidden:
            entry.set_visibility(False)
            entry.set_placeholder_text('Пустое поле сохраняет текущий пароль')
        box.pack_start(entry, False, False, 0)
        grid.attach(box, row % 2, row // 2, 1, 1)
        return entry

    def open_editor(self):
        item = self.selected()
        if item:
            self.load_editor(item)
            self.notebook.set_current_page(1)

    def load_editor(self, item):
        self.current_uuid = item['uuid']
        self.editor_title.set_text('Изменить подключение')
        self.name.set_text(item['name'])
        self.gateway.set_text(item['gateway'])
        self.username.set_text(item['user'])
        self.password.set_text('')

    def create_indicator(self):
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            'sstp-vpn', 'network-vpn', AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.rebuild_tray()

    def rebuild_tray(self):
        menu = Gtk.Menu()
        show = Gtk.MenuItem(label='Открыть SSTP VPN')
        show.connect('activate', lambda _item: self.show_window())
        menu.append(show)
        menu.append(Gtk.SeparatorMenuItem())
        if self.items:
            for item in self.items:
                label = ('Отключить: ' if item['active'] else 'Подключить: ') + item['name']
                entry = Gtk.MenuItem(label=label)
                entry.connect('activate', lambda _item, profile=item: self.toggle_profile(profile))
                menu.append(entry)
        else:
            empty = Gtk.MenuItem(label='SSTP-профилей нет')
            empty.set_sensitive(False)
            menu.append(empty)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label='Выйти')
        quit_item.connect('activate', lambda _item: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)

    def refresh(self, *_args):
        selected_row = self.listbox.get_selected_row()
        selected_uuid = selected_row.profile['uuid'] if selected_row else None
        try:
            self.items = profiles()
            active = [item['name'] for item in self.items if item['active']]
            self.hero_title.set_text('Вы подключены' if active else 'Готовы к подключению')
            self.hero_detail.set_text(' · '.join(active) if active else 'Выберите сохранённую сеть или создайте новую.')
            self.status_label.set_text(f'Сохранено подключений: {len(self.items)}\nАктивных: {len(active)}')
        except Exception as exc:
            self.items = []
            self.hero_title.set_text('Сеть недоступна')
            self.hero_detail.set_text('Не удалось связаться с NetworkManager.')
            self.status_label.set_text('NetworkManager недоступен: ' + str(exc))
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        for item in self.items:
            content = Gtk.Box(spacing=16)
            icon = Gtk.Image.new_from_icon_name('network-vpn-symbolic', Gtk.IconSize.DND)
            content.pack_start(icon, False, False, 2)
            details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            details.pack_start(self.label(item['name'], 'title'), False, False, 0)
            details.pack_start(self.label(f"{item['gateway']}  ·  {item['user']}", 'muted'), False, False, 0)
            content.pack_start(details, True, True, 0)
            badge = self.label('● Подключено' if item['active'] else 'Отключено', 'badge')
            badge.set_valign(Gtk.Align.CENTER)
            if item['active']:
                badge.get_style_context().add_class('connected')
            content.pack_end(badge, False, False, 0)
            row = Gtk.ListBoxRow()
            row.profile = item
            row.add(content)
            self.listbox.add(row)
            if item['uuid'] == selected_uuid:
                self.listbox.select_row(row)
        self.listbox.show_all()
        self.on_row_selected(self.listbox, self.listbox.get_selected_row())
        self.rebuild_tray()

    def selected(self):
        row = self.listbox.get_selected_row()
        if not row:
            self.message('Выберите подключение в списке.', Gtk.MessageType.INFO)
            return None
        return row.profile

    def on_row_selected(self, _box, row):
        for button in self.selection_buttons:
            button.set_sensitive(row is not None)

    def new_profile(self):
        self.editor_title.set_text('Новый профиль')
        self.current_uuid = None
        for entry in (self.name, self.gateway, self.username, self.password):
            entry.set_text('')
        self.notebook.set_current_page(1)
        self.name.grab_focus()

    def matching_sstp_profiles(self, name):
        return [item for item in profiles() if item['name'] == name]

    def save_profile(self):
        name, gateway, user, password = (self.name.get_text().strip(), self.gateway.get_text().strip(),
                                         self.username.get_text().strip(), self.password.get_text())
        if not name or not gateway or not user:
            self.message('Заполните название, сервер и имя пользователя.', Gtk.MessageType.WARNING)
            return
        if not self.unlock_vault():
            return
        if self.current_uuid is None:
            matches = self.matching_sstp_profiles(name)
            if len(matches) > 1:
                self.message('Есть несколько профилей с названием «' + name + '». Выберите нужный в списке и нажмите «Изменить».', Gtk.MessageType.WARNING)
                return
            if matches:
                self.current_uuid = matches[0]['uuid']
            else:
                if not password:
                    self.message('Для нового профиля укажите пароль.', Gtk.MessageType.WARNING)
                    return
                result = run_nmcli('connection', 'add', 'type', 'vpn', 'vpn-type', 'sstp', 'ifname', '*', 'con-name', name)
                if result.returncode:
                    self.error_from(result, 'Не удалось создать профиль.')
                    return
                matches = self.matching_sstp_profiles(name)
                if len(matches) != 1:
                    self.message('Не удалось определить созданный профиль.', Gtk.MessageType.ERROR)
                    return
                self.current_uuid = matches[0]['uuid']
        else:
            result = run_nmcli('connection', 'modify', 'uuid', self.current_uuid, 'connection.id', name)
            if result.returncode:
                self.error_from(result, 'Не удалось изменить название профиля.')
                return
        previous = self.vault['profiles'].get(self.current_uuid, {})
        secret = password or previous.get('password', '')
        if not secret:
            self.message('Введите пароль SSTP, чтобы перенести профиль в зашифрованное хранилище.', Gtk.MessageType.WARNING)
            return
        result = run_nmcli('connection', 'modify', 'uuid', self.current_uuid, 'vpn.data',
                           f'gateway={gateway},user={user},password-flags=2,tls-ext=yes')
        if result.returncode:
            self.error_from(result, 'Не удалось сохранить настройки.')
            return
        # Old profiles may have a NetworkManager secret. Remove it after the
        # encrypted vault has become the source of truth.
        run_nmcli('connection', 'modify', 'uuid', self.current_uuid, '-vpn.secrets', 'password')
        self.vault['profiles'][self.current_uuid] = {'name': name, 'gateway': gateway, 'user': user, 'password': secret}
        try:
            self.write_vault()
        except OSError as exc:
            self.error_from(type('R', (), {'stderr': str(exc), 'stdout': ''})(), 'Не удалось записать зашифрованное хранилище.')
            return
        self.password.set_text('')
        self.refresh()
        self.status_label.set_text(f'Профиль «{name}» сохранён в зашифрованном хранилище.')
        self.message(f'Профиль «{name}» сохранён в зашифрованном хранилище.', Gtk.MessageType.INFO)

    def connect_selected(self, *_args):
        item = self.selected()
        if item:
            self.activate(item, True)

    def disconnect_selected(self, *_args):
        item = self.selected()
        if item:
            self.activate(item, False)

    def toggle_profile(self, item):
        self.activate(item, not item['active'])

    def activate(self, item, enable):
        action = 'up' if enable else 'down'
        if enable:
            if not self.unlock_vault():
                return
            record = self.vault['profiles'].get(item['uuid'])
            if not record:
                self.message('Этот профиль ещё не перенесён в зашифрованное хранилище. Откройте «Изменить», введите пароль и сохраните.', Gtk.MessageType.WARNING)
                return
            fd, password_file = tempfile.mkstemp(prefix='.sstp-', dir=str(VAULT_PATH.parent))
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, ('vpn.secrets.password:' + record['password'] + '\n').encode())
                os.close(fd)
                result = run_nmcli('connection', action, 'uuid', item['uuid'], 'passwd-file', password_file)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(password_file)
                except OSError:
                    pass
        else:
            result = run_nmcli('connection', action, 'uuid', item['uuid'])
        if result.returncode:
            self.error_from(result, f'Не удалось {"подключить" if enable else "отключить"} «{item["name"]}».')
        else:
            self.status_label.set_text(f'«{item["name"]}» {"подключён" if enable else "отключён"}.')
        self.refresh()

    def delete_selected(self, *_args):
        item = self.selected()
        if not item:
            return
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=f'Удалить «{item["name"]}»?')
        dialog.format_secondary_text('Профиль VPN и сохранённый пароль будут удалены.')
        dialog.add_buttons('Отмена', Gtk.ResponseType.CANCEL, 'Удалить', Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        if self.vault is not None and item['uuid'] in self.vault['profiles']:
            del self.vault['profiles'][item['uuid']]
            self.write_vault()
        result = run_nmcli('connection', 'delete', 'uuid', item['uuid'])
        if result.returncode:
            self.error_from(result, 'Не удалось удалить профиль.')
        else:
            self.new_profile()
            self.status_label.set_text(f'Профиль «{item["name"]}» удалён.')
        self.refresh()

    def error_from(self, result, title):
        detail = (result.stderr or result.stdout).strip()
        self.message(title + ('\n\n' + detail if detail else ''), Gtk.MessageType.ERROR)

    def message(self, text, kind):
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=kind,
                                   buttons=Gtk.ButtonsType.OK, text=text)
        dialog.run()
        dialog.destroy()

    def hide_window(self, *_args):
        self.window.hide()
        return True

    def quit(self, *_args):
        # «Выйти» из трея завершает только после отключения SSTP-профилей.
        try:
            active_profiles = [item for item in profiles() if item['active']]
            for item in active_profiles:
                run_nmcli('connection', 'down', 'uuid', item['uuid'])
        except Exception:
            # Завершение приложения не должно зависать, если NetworkManager
            # уже остановлен или недоступен.
            pass
        controller = getattr(self, 'controller', None)
        if controller is not None:
            controller.release()
            controller.quit()
        else:
            Gtk.main_quit()

    def show_window(self):
        self.window.show_all()
        self.window.present()


def activate_running_instance():
    """Ask the D-Bus primary instance to present its existing window."""
    try:
        remote = Gio.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        remote.register(None)
        if remote.get_is_remote():
            remote.activate()
    except GLib.Error:
        pass


def main():
    # flock is the reliable guard: its lock is released by the kernel when the
    # process exits, so it cannot leave stale files behind after a crash.
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_PATH, 'w')
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        activate_running_instance()
        return 0

    controller = Gio.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def activate(application):
        if hasattr(application, 'vpn_window'):
            application.vpn_window.show_window()
            return
        window = VPNApp()
        window.controller = application
        application.vpn_window = window
        application.hold()
        window.show_window()

    controller.connect('activate', activate)
    controller.lock_file = lock_file
    return controller.run(sys.argv)


if __name__ == '__main__':
    raise SystemExit(main())
