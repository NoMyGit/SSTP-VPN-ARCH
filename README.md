# SSTP VPN для Arch Linux

## Установка

Распакуйте папку на новом компьютере, откройте терминал в ней и выполните:

```bash
chmod +x install.sh
./install.sh
```

Установщик ставит NetworkManager, SSTP-плагин и зависимости, затем размещает программу в `~/.local/share/sstp-vpn/` согласно XDG Base Directory.

## Перенос подключений

Данные профилей и пароли шифруются в `~/.local/share/sstp-vpn/connections.vault`.

На старом компьютере в папке архива выполните:

```bash
chmod +x export-vault.sh
./export-vault.sh
```

Скрипт добавит зашифрованный `connections.vault` в эту папку. Перенесите и распакуйте всю папку на новом ПК, затем выполните `./install.sh`.

После запуска SSTP VPN введите прежний мастер-пароль и откройте вкладку «Состояние». Нажмите «Восстановить профили из vault»: приложение создаст SSTP-профили в NetworkManager из зашифрованных данных.

Фон и тема сохраняются отдельно в `~/.config/sstp-vpn/appearance.json`.
