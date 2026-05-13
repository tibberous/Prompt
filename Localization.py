from __future__ import annotations

import configparser
import os
import sys
import traceback
from pathlib import Path
from typing import Any


class Localization:
    """Small shared EN/ES user-facing text layer for launcher/debugger surfaces."""

    EN = 'EN'
    ES = 'ES'
    HI = 'HI'
    ZH = 'ZH'
    RU = 'RU'
    UK = 'UK'
    DE = 'DE'
    FR = 'FR'
    STRINGS: dict[str, dict[str, str]] = {
        EN: {
            'action.view_source': '📄 View Source',
            'action.save_node_python': '🐍 Save Node as Python',
            'action.save_node_html': '🌐 Save Node as HTML',
            'action.save_node_xml': '🧩 Save Node as XML',
            'button.print': 'Print',
            'button.save': 'Save',
            'button.save_pdf': 'Save PDF',
            'button.cancel': 'Cancel',
            'button.close': 'Close',
            'button.save_auth': 'Save Auth',
            'dialog.github_auth': 'GitHub Auth',
            'label.interval_seconds': 'Interval (seconds):',
            'github.auth_info': 'Enter your Git author identity and your GitHub login. For GitHub HTTPS, use a personal access token in the password/token field.',
            'github.default_host': 'github.com',
            'github.default_remote': 'origin',
            'github.setup_git': 'Run gh auth setup-git after login when gh is available',
            'github.author_name': 'Author Name',
            'github.author_email': 'Author Email',
            'github.username': 'GitHub Username',
            'github.password_token': 'Password / Token',
            'github.host': 'Host',
            'github.remote': 'Remote',
        },
        ES: {
            'action.view_source': '📄 Ver código fuente',
            'action.save_node_python': '🐍 Guardar nodo como Python',
            'action.save_node_html': '🌐 Guardar nodo como HTML',
            'action.save_node_xml': '🧩 Guardar nodo como XML',
            'button.print': 'Imprimir',
            'button.save': 'Guardar',
            'button.save_pdf': 'Guardar PDF',
            'button.cancel': 'Cancelar',
            'button.close': 'Cerrar',
            'button.save_auth': 'Guardar autenticación',
            'dialog.github_auth': 'Autenticación de GitHub',
            'label.interval_seconds': 'Intervalo (segundos):',
            'github.auth_info': 'Introduce tu identidad de autor de Git y tu inicio de sesión de GitHub. Para GitHub HTTPS, usa un token de acceso personal en el campo de contraseña/token.',
            'github.default_host': 'github.com',
            'github.default_remote': 'origin',
            'github.setup_git': 'Ejecutar gh auth setup-git después del inicio de sesión cuando gh esté disponible',
            'github.author_name': 'Nombre del autor',
            'github.author_email': 'Correo del autor',
            'github.username': 'Usuario de GitHub',
            'github.password_token': 'Contraseña / token',
            'github.host': 'Host',
            'github.remote': 'Remoto',
        },
        HI: {
            'action.view_source': '📄 स्रोत देखें',
            'action.save_node_python': '🐍 नोड को Python के रूप में सहेजें',
            'action.save_node_html': '🌐 नोड को HTML के रूप में सहेजें',
            'action.save_node_xml': '🧩 नोड को XML के रूप में सहेजें',
            'button.print': 'प्रिंट', 'button.save': 'सहेजें', 'button.save_pdf': 'PDF सहेजें', 'button.cancel': 'रद्द करें', 'button.close': 'बंद करें', 'button.save_auth': 'Auth सहेजें',
            'dialog.github_auth': 'GitHub Auth', 'label.interval_seconds': 'अंतराल (सेकंड):', 'github.auth_info': 'अपनी Git author identity और GitHub login दर्ज करें। GitHub HTTPS के लिए password/token field में personal access token उपयोग करें.',
            'github.default_host': 'github.com', 'github.default_remote': 'origin', 'github.setup_git': 'gh उपलब्ध हो तो login के बाद gh auth setup-git चलाएँ', 'github.author_name': 'लेखक नाम', 'github.author_email': 'लेखक ईमेल', 'github.username': 'GitHub username', 'github.password_token': 'पासवर्ड / token', 'github.host': 'Host', 'github.remote': 'Remote',
        },
        ZH: {
            'action.view_source': '📄 查看源代码',
            'action.save_node_python': '🐍 将节点另存为 Python',
            'action.save_node_html': '🌐 将节点另存为 HTML',
            'action.save_node_xml': '🧩 将节点另存为 XML',
            'button.print': '打印', 'button.save': '保存', 'button.save_pdf': '保存 PDF', 'button.cancel': '取消', 'button.close': '关闭', 'button.save_auth': '保存认证',
            'dialog.github_auth': 'GitHub 认证', 'label.interval_seconds': '间隔（秒）:', 'github.auth_info': '输入您的 Git 作者身份和 GitHub 登录信息。对于 GitHub HTTPS，请在密码/token 字段中使用 personal access token。',
            'github.default_host': 'github.com', 'github.default_remote': 'origin', 'github.setup_git': 'gh 可用时，登录后运行 gh auth setup-git', 'github.author_name': '作者姓名', 'github.author_email': '作者邮箱', 'github.username': 'GitHub 用户名', 'github.password_token': '密码 / Token', 'github.host': '主机', 'github.remote': '远程',
        },
        RU: {
            'action.view_source': '📄 Показать исходник', 'action.save_node_python': '🐍 Сохранить узел как Python', 'action.save_node_html': '🌐 Сохранить узел как HTML', 'action.save_node_xml': '🧩 Сохранить узел как XML',
            'button.print': 'Печать', 'button.save': 'Сохранить', 'button.save_pdf': 'Сохранить PDF', 'button.cancel': 'Отмена', 'button.close': 'Закрыть', 'button.save_auth': 'Сохранить авторизацию',
            'dialog.github_auth': 'Авторизация GitHub', 'label.interval_seconds': 'Интервал (секунды):', 'github.auth_info': 'Введите данные автора Git и логин GitHub. Для GitHub HTTPS используйте personal access token в поле password/token.',
            'github.default_host': 'github.com', 'github.default_remote': 'origin', 'github.setup_git': 'Запустите gh auth setup-git после входа, если gh доступен', 'github.author_name': 'Имя автора', 'github.author_email': 'Email автора', 'github.username': 'Имя пользователя GitHub', 'github.password_token': 'Пароль / token', 'github.host': 'Хост', 'github.remote': 'Remote',
        },
        UK: {
            'action.view_source': '📄 Переглянути джерело', 'action.save_node_python': '🐍 Зберегти вузол як Python', 'action.save_node_html': '🌐 Зберегти вузол як HTML', 'action.save_node_xml': '🧩 Зберегти вузол як XML',
            'button.print': 'Друк', 'button.save': 'Зберегти', 'button.save_pdf': 'Зберегти PDF', 'button.cancel': 'Скасувати', 'button.close': 'Закрити', 'button.save_auth': 'Зберегти авторизацію',
            'dialog.github_auth': 'Авторизація GitHub', 'label.interval_seconds': 'Інтервал (секунди):', 'github.auth_info': 'Введіть Git author identity і GitHub login. Для GitHub HTTPS використовуйте personal access token у полі password/token.',
            'github.default_host': 'github.com', 'github.default_remote': 'origin', 'github.setup_git': 'Запустіть gh auth setup-git після входу, якщо gh доступний', 'github.author_name': 'Ім’я автора', 'github.author_email': 'Email автора', 'github.username': 'Користувач GitHub', 'github.password_token': 'Пароль / token', 'github.host': 'Хост', 'github.remote': 'Remote',
        },
        DE: {
            'action.view_source': '📄 Quelle anzeigen', 'action.save_node_python': '🐍 Knoten als Python speichern', 'action.save_node_html': '🌐 Knoten als HTML speichern', 'action.save_node_xml': '🧩 Knoten als XML speichern',
            'button.print': 'Drucken', 'button.save': 'Speichern', 'button.save_pdf': 'PDF speichern', 'button.cancel': 'Abbrechen', 'button.close': 'Schließen', 'button.save_auth': 'Auth speichern',
            'dialog.github_auth': 'GitHub-Auth', 'label.interval_seconds': 'Intervall (Sekunden):', 'github.auth_info': 'Geben Sie Ihre Git-Autorenidentität und Ihren GitHub-Login ein. Für GitHub HTTPS verwenden Sie im Passwort/Token-Feld ein personal access token.',
            'github.default_host': 'github.com', 'github.default_remote': 'origin', 'github.setup_git': 'Nach dem Login gh auth setup-git ausführen, wenn gh verfügbar ist', 'github.author_name': 'Autorenname', 'github.author_email': 'Autoren-E-Mail', 'github.username': 'GitHub-Benutzername', 'github.password_token': 'Passwort / Token', 'github.host': 'Host', 'github.remote': 'Remote',
        },
        FR: {
            'action.view_source': '📄 Voir la source', 'action.save_node_python': '🐍 Enregistrer le nœud en Python', 'action.save_node_html': '🌐 Enregistrer le nœud en HTML', 'action.save_node_xml': '🧩 Enregistrer le nœud en XML',
            'button.print': 'Imprimer', 'button.save': 'Enregistrer', 'button.save_pdf': 'Enregistrer le PDF', 'button.cancel': 'Annuler', 'button.close': 'Fermer', 'button.save_auth': 'Enregistrer l’authentification',
            'dialog.github_auth': 'Authentification GitHub', 'label.interval_seconds': 'Intervalle (secondes) :', 'github.auth_info': 'Saisissez votre identité d’auteur Git et votre connexion GitHub. Pour GitHub HTTPS, utilisez un personal access token dans le champ mot de passe/token.',
            'github.default_host': 'github.com', 'github.default_remote': 'origin', 'github.setup_git': 'Exécuter gh auth setup-git après la connexion lorsque gh est disponible', 'github.author_name': 'Nom de l’auteur', 'github.author_email': 'Email de l’auteur', 'github.username': 'Nom d’utilisateur GitHub', 'github.password_token': 'Mot de passe / token', 'github.host': 'Hôte', 'github.remote': 'Remote',
        },
    }

    @classmethod
    def normalize(cls, language: Any = None) -> str:
        raw = str(language if language is not None else os.environ.get('PROMPT_LANGUAGE', '')).strip().upper()
        if raw in {'ES', 'ESP', 'SPANISH', 'ESPAÑOL', 'ESPANOL'}:
            return cls.ES
        if raw in {'HI', 'HIN', 'HINDI', 'INDIAN', 'INDIA'}:
            return cls.HI
        if raw in {'ZH', 'CN', 'ZH-CN', 'CHINESE', 'SIMPLIFIED CHINESE', '中文', '简体中文'}:
            return cls.ZH
        if raw in {'RU', 'RUS', 'RUSSIAN', 'РУССКИЙ'}:
            return cls.RU
        if raw in {'UK', 'UA', 'UKR', 'UKRAINIAN', 'УКРАЇНСЬКА'}:
            return cls.UK
        if raw in {'DE', 'GER', 'GERMAN', 'DEUTSCH'}:
            return cls.DE
        if raw in {'FR', 'FRE', 'FRENCH', 'FRANÇAIS', 'FRANCAIS'}:
            return cls.FR
        return cls.EN

    @classmethod
    def fromConfig(cls, root: Any = None) -> str:
        env_language = str(os.environ.get('PROMPT_LANGUAGE', '') or '').strip()
        if env_language:
            return cls.normalize(env_language)
        try:
            config_path = Path(root or Path.cwd()) / 'config.ini'
            if config_path.exists():
                parser = configparser.ConfigParser()
                parser.read(config_path, encoding='utf-8')
                for section, option in (('ui', 'language'), ('prompt', 'language'), ('language', 'current')):
                    if parser.has_option(section, option):
                        return cls.normalize(parser.get(section, option, fallback=''))
        except Exception as error:
            print(f"[WARN:swallowed-exception] Localization.fromConfig {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return cls.EN
        return cls.EN

    @classmethod
    def text(cls, key: str, language: Any = None, **kwargs: Any) -> str:
        lang = cls.normalize(language if language is not None else cls.fromConfig(Path(__file__).resolve().parent))
        value = cls.STRINGS.get(lang, cls.STRINGS[cls.EN]).get(str(key), cls.STRINGS[cls.EN].get(str(key), str(key)))
        try:
            return str(value).format(**kwargs)
        except Exception as error:
            print(f"[WARN:swallowed-exception] Localization.text {type(error).__name__}: {error}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            return str(value)
