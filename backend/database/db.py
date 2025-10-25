import sqlite3
import os
import threading
import logging
import hashlib
import secrets
import base64
from typing import List, Dict, Optional, Callable, Any
from datetime import datetime
import traceback

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover - optional dependency
    psycopg2 = None
    RealDictCursor = None

try:
    import pymysql
    from pymysql.cursors import DictCursor as MySQLDictCursor
except ImportError:  # pragma: no cover - optional dependency
    pymysql = None
    MySQLDictCursor = None

from cryptography.fernet import Fernet, InvalidToken

from utils.email.logger import logger, log_progress

# 配置日志
logger = logging.getLogger('database')

class Database:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(Database, cls).__new__(cls)
                instance = cls._instance
                instance.conn = None
                instance.cursor_factory = None
                instance.paramstyle = '?'
                instance.db_type = os.getenv('DB_TYPE', 'sqlite').lower()
                instance.data_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'data'
                )
                instance.db_path = os.path.join(instance.data_dir, 'huohuo_email.db')
                instance.db_name = None

                logger.info(f"初始化数据库连接, 类型: {instance.db_type}")
                instance.connect_db(instance.db_path if instance.db_type == 'sqlite' else None)
                instance.init_db()
                instance._load_encryption_key()
                instance._ensure_sensitive_data_encrypted()

                return instance
            return cls._instance

    def connect_db(self, db_path: Optional[str] = None):
        """建立数据库连接"""
        if self.db_type == 'sqlite':
            if db_path is None:
                db_path = self.db_path
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self.db_path = db_path
            logger.info(f"连接SQLite数据库: {db_path}")
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.paramstyle = '?'
            self.db_name = db_path
            return

        if self.db_type in ('postgresql', 'postgres'):
            if psycopg2 is None:
                raise RuntimeError('psycopg2 未安装，无法连接PostgreSQL')

            db_url = os.getenv('DB_URL')
            if db_url:
                logger.info('使用DB_URL连接PostgreSQL')
                self.conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
            else:
                host = os.getenv('DB_HOST', 'localhost')
                port = int(os.getenv('DB_PORT', '5432'))
                database = os.getenv('DB_NAME', 'firemail')
                user = os.getenv('DB_USER', 'firemail')
                password = os.getenv('DB_PASSWORD', 'firemail')
                logger.info(f"连接PostgreSQL数据库: {host}:{port}/{database}")
                self.conn = psycopg2.connect(
                    host=host,
                    port=port,
                    dbname=database,
                    user=user,
                    password=password,
                    cursor_factory=RealDictCursor
                )
            self.paramstyle = '%s'
            self.db_name = self.conn.get_dsn_parameters().get('dbname')
            return

        if self.db_type == 'mysql':
            if pymysql is None:
                raise RuntimeError('pymysql 未安装，无法连接MySQL')

            host = os.getenv('DB_HOST', 'localhost')
            port = int(os.getenv('DB_PORT', '3306'))
            database = os.getenv('DB_NAME', 'firemail')
            user = os.getenv('DB_USER', 'firemail')
            password = os.getenv('DB_PASSWORD', 'firemail')
            logger.info(f"连接MySQL数据库: {host}:{port}/{database}")
            self.conn = pymysql.connect(
                host=host,
                port=port,
                db=database,
                user=user,
                password=password,
                charset='utf8mb4',
                cursorclass=MySQLDictCursor,
                autocommit=False
            )
            self.paramstyle = '%s'
            self.db_name = database
            return

        raise ValueError(f"不支持的数据库类型: {self.db_type}")

    def init_db(self):
        """初始化数据库连接和表结构"""
        try:
            statements = self._get_schema_statements()
            for statement in statements:
                self._execute(statement, commit=True)

            # 检查并添加新字段（用于旧版本升级）
            self._check_and_add_column('emails', 'enable_realtime_check', 'INTEGER DEFAULT 0')
            self._check_and_add_column('users', 'password_hash', 'TEXT NOT NULL')

            logger.info("初始化数据库表结构完成")

            # 初始化系统配置
            self._init_system_config()

        except Exception as e:
            logger.error(f"初始化数据库表结构失败: {str(e)}")
            traceback.print_exc()

    def _check_and_add_column(self, table, column, type_def):
        """检查表中是否存在某列，如果不存在则添加"""
        try:
            params = self._column_query_params(table)
            cursor = self._execute(self._column_query(table), params, commit=False)
            columns = [row['name'] for row in cursor.fetchall()]

            if column not in columns:
                logger.info(f"向表 {table} 添加列 {column}")
                alter_sql = f"ALTER TABLE {table} ADD COLUMN {column} {type_def}"
                self._execute(alter_sql, commit=True)
        except Exception as e:
            logger.error(f"检查和添加列失败: {str(e)}")

    def _column_query(self, table: str) -> str:
        if self.db_type == 'sqlite':
            return f"PRAGMA table_info({table})"
        if self.db_type in ('postgresql', 'postgres'):
            return """
                SELECT column_name AS name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = ?
            """
        if self.db_type == 'mysql':
            return """
                SELECT COLUMN_NAME AS name
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            """
        raise ValueError(f"不支持的数据库类型: {self.db_type}")

    def _column_query_params(self, table: str) -> Optional[List[Any]]:
        if self.db_type == 'sqlite':
            return None
        if self.db_type in ('postgresql', 'postgres'):
            return [table]
        if self.db_type == 'mysql':
            return [self.db_name, table]
        raise ValueError(f"不支持的数据库类型: {self.db_type}")

    def _get_schema_statements(self) -> List[str]:
        if self.db_type == 'sqlite':
            return [
                '''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    is_admin INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    password TEXT NOT NULL,
                    mail_type TEXT DEFAULT 'outlook',
                    server TEXT,
                    port INTEGER,
                    use_ssl INTEGER DEFAULT 1,
                    client_id TEXT,
                    refresh_token TEXT,
                    access_token TEXT,
                    last_check_time TIMESTAMP,
                    enable_realtime_check INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id),
                    UNIQUE (user_id, email)
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS mail_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_id INTEGER NOT NULL,
                    subject TEXT,
                    sender TEXT,
                    received_time TIMESTAMP,
                    content TEXT,
                    folder TEXT,
                    has_attachments INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (email_id) REFERENCES emails (id)
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mail_id INTEGER NOT NULL,
                    filename TEXT,
                    content_type TEXT,
                    size INTEGER,
                    content BLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (mail_id) REFERENCES mail_records (id) ON DELETE CASCADE
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS system_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                '''
            ]

        if self.db_type in ('postgresql', 'postgres'):
            return [
                '''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    is_admin BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS emails (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    password TEXT NOT NULL,
                    mail_type TEXT DEFAULT 'outlook',
                    server TEXT,
                    port INTEGER,
                    use_ssl BOOLEAN DEFAULT TRUE,
                    client_id TEXT,
                    refresh_token TEXT,
                    access_token TEXT,
                    last_check_time TIMESTAMP,
                    enable_realtime_check BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id),
                    UNIQUE (user_id, email)
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS mail_records (
                    id SERIAL PRIMARY KEY,
                    email_id INTEGER NOT NULL,
                    subject TEXT,
                    sender TEXT,
                    received_time TIMESTAMP,
                    content TEXT,
                    folder TEXT,
                    has_attachments BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (email_id) REFERENCES emails (id)
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS attachments (
                    id SERIAL PRIMARY KEY,
                    mail_id INTEGER NOT NULL,
                    filename TEXT,
                    content_type TEXT,
                    size INTEGER,
                    content BYTEA,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (mail_id) REFERENCES mail_records (id) ON DELETE CASCADE
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS system_config (
                    id SERIAL PRIMARY KEY,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                '''
            ]

        if self.db_type == 'mysql':
            return [
                '''
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(255) UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    is_admin TINYINT(1) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                ''',
                '''
                CREATE TABLE IF NOT EXISTS emails (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    password TEXT NOT NULL,
                    mail_type VARCHAR(50) DEFAULT 'outlook',
                    server VARCHAR(255),
                    port INT,
                    use_ssl TINYINT(1) DEFAULT 1,
                    client_id TEXT,
                    refresh_token TEXT,
                    access_token TEXT,
                    last_check_time TIMESTAMP NULL DEFAULT NULL,
                    enable_realtime_check TINYINT(1) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_user_email (user_id, email),
                    CONSTRAINT fk_emails_users FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                ''',
                '''
                CREATE TABLE IF NOT EXISTS mail_records (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    email_id INT NOT NULL,
                    subject TEXT,
                    sender TEXT,
                    received_time TIMESTAMP NULL DEFAULT NULL,
                    content LONGTEXT,
                    folder TEXT,
                    has_attachments TINYINT(1) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT fk_mail_records_emails FOREIGN KEY (email_id) REFERENCES emails (id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                ''',
                '''
                CREATE TABLE IF NOT EXISTS attachments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    mail_id INT NOT NULL,
                    filename TEXT,
                    content_type TEXT,
                    size BIGINT,
                    content LONGBLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT fk_attachments_mail FOREIGN KEY (mail_id) REFERENCES mail_records (id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                ''',
                '''
                CREATE TABLE IF NOT EXISTS system_config (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    `key` VARCHAR(255) UNIQUE NOT NULL,
                    value TEXT,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                '''
            ]

        raise ValueError(f"不支持的数据库类型: {self.db_type}")

    def _cursor(self):
        return self.conn.cursor()

    def _prepare_query(self, query: str) -> str:
        if self.paramstyle == '%s':
            return query.replace('?', '%s')
        return query

    def _bool_value(self, value: Any) -> Any:
        if value is None:
            return None
        truthy = bool(value)
        if self.db_type in ('postgresql', 'postgres'):
            return truthy
        return 1 if truthy else 0

    def _execute(self, query: str, params: Optional[List[Any]] = None, commit: bool = False):
        cursor = self._cursor()
        prepared_query = self._prepare_query(query)
        try:
            if params is None:
                cursor.execute(prepared_query)
            else:
                cursor.execute(prepared_query, tuple(params))
            if commit:
                self.conn.commit()
            return cursor
        except Exception as e:
            logger.error(f"执行SQL失败: {str(e)} | SQL: {query} | 参数: {params}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def _get_last_insert_id(self, table: str) -> Optional[int]:
        if self.db_type == 'sqlite':
            cursor = self._execute('SELECT last_insert_rowid()', commit=False)
            row = cursor.fetchone()
            if row is None:
                return None
            if isinstance(row, dict):
                return list(row.values())[0]
            return row[0]

        if self.db_type in ('postgresql', 'postgres'):
            cursor = self._execute('SELECT LASTVAL()', commit=False)
            row = cursor.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return list(row.values())[0]
            return row[0]

        if self.db_type == 'mysql':
            cursor = self._execute('SELECT LAST_INSERT_ID()', commit=False)
            row = cursor.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return list(row.values())[0]
            return row[0]

        raise ValueError(f"不支持的数据库类型: {self.db_type}")

    def _row_to_dict(self, row: Any) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        if isinstance(row, dict):
            return dict(row)
        try:
            return dict(row)
        except TypeError:
            if hasattr(row, 'keys'):
                return {key: row[key] for key in row.keys()}
            return None

    def _load_encryption_key(self):
        if hasattr(self, 'fernet') and getattr(self, 'fernet', None):
            return

        key = os.getenv('DB_ENCRYPTION_KEY')
        if key:
            key_bytes = self._normalize_key(key)
        else:
            os.makedirs(self.data_dir, exist_ok=True)
            key_path = os.path.join(self.data_dir, 'db_encryption.key')
            if os.path.exists(key_path):
                with open(key_path, 'rb') as key_file:
                    key_bytes = key_file.read().strip()
            else:
                key_bytes = Fernet.generate_key()
                with open(key_path, 'wb') as key_file:
                    key_file.write(key_bytes)
                logger.info(f"生成新的数据库加密密钥: {key_path}")

        self.fernet = Fernet(key_bytes)

    def _normalize_key(self, key: Any) -> bytes:
        if isinstance(key, str):
            key = key.encode('utf-8')
        try:
            Fernet(key)
            return key
        except (ValueError, TypeError):
            return base64.urlsafe_b64encode(hashlib.sha256(key).digest())

    def _encrypt_value(self, value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, bytes):
            payload = value
        else:
            payload = str(value).encode('utf-8')
        token = self.fernet.encrypt(payload)
        return token.decode('utf-8')

    def _decrypt_value(self, value: Optional[Any]) -> Optional[str]:
        if value in (None, ''):
            return value
        try:
            token = value if isinstance(value, bytes) else value.encode('utf-8')
            decrypted = self.fernet.decrypt(token)
            return decrypted.decode('utf-8')
        except (InvalidToken, ValueError, TypeError):
            return value

    def _encrypt_bytes(self, value: Optional[Any]) -> Optional[bytes]:
        if value is None:
            return None
        if isinstance(value, memoryview):
            value = value.tobytes()
        if not isinstance(value, bytes):
            value = bytes(value)
        return self.fernet.encrypt(value)

    def _decrypt_bytes(self, value: Optional[Any]) -> Optional[bytes]:
        if value is None:
            return None
        if isinstance(value, memoryview):
            value = value.tobytes()
        if not isinstance(value, bytes):
            value = bytes(value)
        try:
            return self.fernet.decrypt(value)
        except (InvalidToken, ValueError, TypeError):
            return value

    def _is_encrypted(self, value: Optional[Any]) -> bool:
        if value in (None, ''):
            return True
        try:
            token = value if isinstance(value, bytes) else value.encode('utf-8')
            self.fernet.decrypt(token)
            return True
        except (InvalidToken, ValueError, TypeError):
            return False

    def _is_encrypted_bytes(self, value: Optional[Any]) -> bool:
        if value is None:
            return True
        if isinstance(value, memoryview):
            value = value.tobytes()
        if not isinstance(value, bytes):
            value = bytes(value)
        try:
            self.fernet.decrypt(value)
            return True
        except (InvalidToken, ValueError, TypeError):
            return False

    def _ensure_sensitive_data_encrypted(self):
        try:
            cursor = self._execute(
                "SELECT id, password, client_id, refresh_token, access_token FROM emails",
                commit=False
            )
            rows = cursor.fetchall()
            for row in rows:
                row_dict = self._row_to_dict(row)
                if not row_dict:
                    continue
                updates = {}
                for field in ['password', 'client_id', 'refresh_token', 'access_token']:
                    value = row_dict.get(field)
                    if value and not self._is_encrypted(value):
                        updates[field] = self._encrypt_value(value)

                if updates:
                    updates['updated_at'] = datetime.now()
                    set_clause = ', '.join([f"{key} = ?" for key in updates.keys()])
                    params = list(updates.values()) + [row_dict['id']]
                    self._execute(
                        f"UPDATE emails SET {set_clause} WHERE id = ?",
                        params,
                        commit=True
                    )

            cursor = self._execute(
                "SELECT id, content FROM attachments",
                commit=False
            )
            rows = cursor.fetchall()
            for row in rows:
                row_dict = self._row_to_dict(row)
                if not row_dict:
                    continue
                content = row_dict.get('content')
                if content and not self._is_encrypted_bytes(content):
                    encrypted = self._encrypt_bytes(content)
                    self._execute(
                        "UPDATE attachments SET content = ?, created_at = created_at WHERE id = ?",
                        [encrypted, row_dict['id']],
                        commit=True
                    )
        except Exception as e:
            logger.error(f"加密已有敏感数据时发生错误: {str(e)}")

    def _decrypt_email_record(self, row: Any) -> Dict[str, Any]:
        record = self._row_to_dict(row) or {}
        for field in ['password', 'client_id', 'refresh_token', 'access_token']:
            if field in record:
                record[field] = self._decrypt_value(record[field])
        if 'use_ssl' in record and record['use_ssl'] is not None:
            record['use_ssl'] = bool(record['use_ssl'])
        if 'enable_realtime_check' in record and record['enable_realtime_check'] is not None:
            record['enable_realtime_check'] = bool(record['enable_realtime_check'])
        return record

    def _init_system_config(self):
        """初始化系统配置"""
        # 检查是否存在注册配置，默认为开启
        cursor = self._execute("SELECT value FROM system_config WHERE key = 'allow_register'", commit=False)
        result = cursor.fetchone()
        if not result:
            logger.info("初始化系统配置: 默认允许注册")
            self._execute(
                "INSERT INTO system_config (key, value, created_at, updated_at) VALUES ('allow_register', 'true', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                commit=True
            )
        else:
            # 确保注册功能默认开启，防止旧数据导致无法注册
            if result['value'] != 'true':
                logger.info("重置系统配置: 默认允许注册")
                self._execute(
                    "UPDATE system_config SET value = 'true', updated_at = CURRENT_TIMESTAMP WHERE key = 'allow_register'",
                    commit=True
                )

    def get_system_config(self, key):
        """获取系统配置"""
        try:
            cursor = self._execute("SELECT value FROM system_config WHERE key = ?", [key], commit=False)
            result = cursor.fetchone()
            return result['value'] if result else None
        except Exception as e:
            logger.error(f"获取系统配置失败: key={key}, 错误: {str(e)}")
            return None

    def set_system_config(self, key, value):
        """设置系统配置"""
        try:
            existing = self._execute(
                "SELECT id FROM system_config WHERE key = ?",
                [key],
                commit=False
            ).fetchone()

            if existing:
                self._execute(
                    "UPDATE system_config SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
                    [value, key],
                    commit=True
                )
            else:
                self._execute(
                    "INSERT INTO system_config (key, value, created_at, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    [key, value],
                    commit=True
                )
            logger.info(f"系统配置已更新: {key} = {value}")
            return True
        except Exception as e:
            logger.error(f"更新系统配置失败: {key} = {value}, 错误: {str(e)}")
            return False

    def is_registration_allowed(self):
        """检查是否允许注册"""
        try:
            allow_register = self.get_system_config('allow_register')
            # 如果配置不存在，默认允许注册
            if allow_register is None:
                logger.info("注册配置不存在，设置为默认允许")
                success = self.set_system_config('allow_register', 'true')
                if not success:
                    logger.warning("设置默认注册配置失败，仍然默认允许注册")
                return True

            logger.info(f"读取到注册配置: {allow_register}")
            return allow_register.lower() == 'true'
        except Exception as e:
            # 出现异常时，确保默认允许注册
            logger.error(f"检查注册状态时出错: {str(e)}，默认允许注册")
            return True

    def toggle_registration(self, allow):
        """开启或关闭注册功能"""
        value = 'true' if allow else 'false'
        logger.info(f"正在{'开启' if allow else '关闭'}注册功能")
        result = self.set_system_config('allow_register', value)
        if result:
            logger.info(f"注册功能已成功切换为: {value}")
        else:
            logger.error(f"切换注册功能失败，目标状态: {value}")
        return result

    def _hash_password(self, password, salt):
        """密码哈希"""
        return hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            salt.encode('utf-8'),
            100000
        ).hex()

    # 用户相关方法
    def authenticate_user(self, username, password):
        """验证用户凭据"""
        cursor = self._execute(
            "SELECT id, username, password, password_hash, salt, is_admin FROM users WHERE username = ?",
            [username],
            commit=False
        )
        user = self._row_to_dict(cursor.fetchone())
        if not user:
            logger.warning(f"用户不存在: {username}")
            return None

        # 尝试新的密码哈希验证方式
        if user['password_hash'] and user['salt']:
            password_hash = self._hash_password(password, user['salt'])
            if password_hash == user['password_hash']:
                logger.info(f"用户 {username} 使用密码哈希验证成功")
                return {'id': user['id'], 'username': user['username'], 'is_admin': user['is_admin']}

        # 如果未设置哈希或哈希验证失败，尝试使用旧的方式（直接比较密码）
        if user['password'] == password:
            logger.info(f"用户 {username} 使用旧密码格式验证成功，将自动升级到哈希格式")
            # 自动升级到新的密码哈希格式
            try:
                salt = secrets.token_hex(16)
                password_hash = self._hash_password(password, salt)
                self._execute(
                    "UPDATE users SET password_hash = ?, salt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [password_hash, salt, user['id']],
                    commit=True
                )
                logger.info(f"用户 {username} 密码已自动升级到哈希格式")
            except Exception as e:
                logger.error(f"自动升级密码格式失败: {str(e)}")

            return {'id': user['id'], 'username': user['username'], 'is_admin': user['is_admin']}

        logger.warning(f"用户密码错误: {username}")
        return None

    def get_user_by_id(self, user_id):
        """根据ID获取用户信息"""
        cursor = self._execute(
            "SELECT id, username, is_admin FROM users WHERE id = ?",
            [user_id],
            commit=False
        )
        return cursor.fetchone()

    def create_user(self, username, password, is_admin=False):
        """创建新用户"""
        try:
            # 检查是否需要将此用户设置为管理员（如果是第一个注册的用户）
            if not is_admin:
                cursor = self._execute("SELECT COUNT(*) AS total FROM users", commit=False)
                count_row = cursor.fetchone()
                total_users = 0
                if count_row:
                    total_users = count_row['total'] if isinstance(count_row, dict) else count_row[0]
                if total_users == 0:
                    is_admin = True
                    logger.info(f"第一个注册的用户 {username} 将被设置为管理员")

            existing = self._execute(
                "SELECT 1 FROM users WHERE username = ?",
                [username],
                commit=False
            ).fetchone()
            if existing:
                logger.warning(f"用户已存在: {username}")
                return False, False

            salt = secrets.token_hex(16)
            password_hash = self._hash_password(password, salt)

            self._execute(
                "INSERT INTO users (username, password, password_hash, salt, is_admin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (username, password, password_hash, salt, self._bool_value(is_admin)),
                commit=True
            )
            logger.info(f"创建用户成功: {username}, 管理员权限: {is_admin}")
            return True, is_admin
        except Exception as e:
            logger.error(f"创建用户失败: {str(e)}")
            raise

    def update_user_password(self, user_id, new_password):
        """更新用户密码"""
        try:
            salt = secrets.token_hex(16)
            password_hash = self._hash_password(new_password, salt)

            self._execute(
                "UPDATE users SET password = ?, password_hash = ?, salt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [new_password, password_hash, salt, user_id],
                commit=True
            )
            logger.info(f"用户ID {user_id} 密码更新成功")
            return True
        except Exception as e:
            logger.error(f"更新用户密码失败: {str(e)}")
            return False

    def delete_user(self, user_id):
        """删除用户"""
        try:
            # 先获取用户关联的所有邮箱
            cursor = self._execute("SELECT id FROM emails WHERE user_id = ?", [user_id], commit=False)
            email_ids = [row['id'] for row in cursor.fetchall()]

            # 删除邮件记录
            if email_ids:
                placeholders = ','.join(['?'] * len(email_ids))
                self._execute(f"DELETE FROM mail_records WHERE email_id IN ({placeholders})", email_ids, commit=True)

            # 删除邮箱
            self._execute("DELETE FROM emails WHERE user_id = ?", [user_id], commit=True)

            # 删除用户
            self._execute("DELETE FROM users WHERE id = ?", [user_id], commit=True)
            logger.info(f"用户ID {user_id} 删除成功")
            return True
        except Exception as e:
            logger.error(f"删除用户失败: {str(e)}")
            return False

    def get_all_users(self):
        """获取所有用户"""
        cursor = self._execute("SELECT id, username, is_admin, created_at FROM users ORDER BY created_at DESC", commit=False)
        return cursor.fetchall()

    # 邮箱相关方法
    def add_email(self, user_id, email, password, client_id=None, refresh_token=None, mail_type='outlook', server=None, port=None, use_ssl=True):
        """添加新的邮箱账号"""
        try:
            # 日志输出详细信息，但隐藏敏感信息
            if mail_type == 'outlook':
                logger.info(f"尝试添加Outlook邮箱: {email} (用户ID: {user_id})")
            elif mail_type == 'imap':
                logger.info(f"尝试添加IMAP邮箱: {email} (用户ID: {user_id}, 服务器: {server}:{port}, SSL: {use_ssl})")
            else:
                logger.info(f"尝试添加邮箱: {email} (用户ID: {user_id}, 类型: {mail_type})")

            # 检查邮箱是否已存在
            existing = self._execute(
                "SELECT 1 FROM emails WHERE user_id = ? AND email = ?",
                [user_id, email],
                commit=False
            ).fetchone()
            if existing:
                logger.warning(f"邮箱已存在: {email}")
                return False

            encrypted_password = self._encrypt_value(password) if password else None
            encrypted_client_id = self._encrypt_value(client_id) if client_id else None
            encrypted_refresh_token = self._encrypt_value(refresh_token) if refresh_token else None

            # 根据邮箱类型处理SQL，默认启用实时检查
            if mail_type == 'outlook':
                self._execute(
                    "INSERT INTO emails (user_id, email, password, client_id, refresh_token, mail_type, enable_realtime_check, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    [
                        user_id,
                        email,
                        encrypted_password,
                        encrypted_client_id,
                        encrypted_refresh_token,
                        mail_type,
                        self._bool_value(True),
                    ],
                    commit=True
                )
            elif mail_type in ['imap', 'gmail', 'qq']:
                self._execute(
                    "INSERT INTO emails (user_id, email, password, mail_type, server, port, use_ssl, enable_realtime_check, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    [
                        user_id,
                        email,
                        encrypted_password,
                        mail_type,
                        server,
                        port,
                        self._bool_value(use_ssl),
                        self._bool_value(True),
                    ],
                    commit=True
                )
            else:
                logger.error(f"不支持的邮箱类型: {mail_type}")
                return False

            email_id = self._get_last_insert_id('emails')
            logger.info(f"邮箱添加成功: {email}, ID: {email_id}, 类型: {mail_type}, 已启用实时检查")
            return email_id
        except Exception as e:
            logger.error(f"添加邮箱失败: {email}, 错误: {str(e)}")
            return False

    def get_all_emails(self, user_id=None):
        """获取所有邮箱账号，可以按用户ID过滤"""
        logger.debug(f"获取所有邮箱账号 (用户ID: {user_id if user_id else 'all'})")
        if user_id:
            cursor = self._execute(
                "SELECT * FROM emails WHERE user_id = ? ORDER BY created_at DESC",
                [user_id],
                commit=False
            )
        else:
            cursor = self._execute("SELECT * FROM emails ORDER BY created_at DESC", commit=False)
        return [self._decrypt_email_record(row) for row in cursor.fetchall()]

    def get_emails_by_user_id(self, user_id):
        """根据用户ID获取所有邮箱账号"""
        logger.debug(f"获取用户ID: {user_id} 的所有邮箱账号")
        cursor = self._execute(
            "SELECT * FROM emails WHERE user_id = ? ORDER BY created_at DESC",
            [user_id],
            commit=False
        )
        return [self._decrypt_email_record(row) for row in cursor.fetchall()]

    def get_email_by_id(self, email_id, user_id=None):
        """根据ID获取邮箱账号，可以验证所有者"""
        logger.debug(f"获取邮箱 ID: {email_id}")
        try:
            if user_id:
                cursor = self._execute(
                    "SELECT * FROM emails WHERE id = ? AND user_id = ?",
                    [email_id, user_id],
                    commit=False
                )
            else:
                cursor = self._execute("SELECT * FROM emails WHERE id = ?", [email_id], commit=False)

            email_info = self._decrypt_email_record(cursor.fetchone())
            if not email_info:
                logger.warning(f"未找到邮箱信息，ID: {email_id}")
                return None

            if email_info.get('mail_type') == 'imap':
                logger.debug(f"获取到IMAP邮箱配置，邮箱ID: {email_id}, 服务器: {email_info.get('server', 'N/A')}:{email_info.get('port', 'N/A')}, SSL: {email_info.get('use_ssl')}")

            return email_info
        except Exception as e:
            logger.error(f"获取邮箱信息失败，ID: {email_id}, 错误: {str(e)}")
            return None

    def update_email(self, email_id, user_id=None, **kwargs):
        """更新邮箱信息"""
        try:
            # 构建更新语句
            update_fields = []
            params = []

            # 处理每个更新字段
            for key, value in kwargs.items():
                if key in {'use_ssl', 'enable_realtime_check', 'has_attachments', 'is_admin'} and value is not None:
                    value = self._bool_value(value)
                if key in ['password', 'client_id', 'refresh_token', 'access_token'] and value is not None:
                    value = self._encrypt_value(value)
                update_fields.append(f"{key} = ?")
                params.append(value)

            # 添加更新时间
            update_fields.append("updated_at = CURRENT_TIMESTAMP")

            # 构建WHERE条件
            where_condition = "id = ?"
            params.append(email_id)

            # 如果指定了用户ID，添加用户ID验证
            if user_id:
                where_condition += " AND user_id = ?"
                params.append(user_id)

            # 执行更新
            sql = f"""
                UPDATE emails
                SET {', '.join(update_fields)}
                WHERE {where_condition}
            """

            self._execute(sql, params, commit=True)
            logger.info(f"邮箱信息更新成功: ID={email_id}")
            return True

        except Exception as e:
            logger.error(f"更新邮箱信息失败: {str(e)}")
            return False

    def update_check_time(self, email_id):
        """更新邮箱的最后检查时间"""
        logger.debug(f"更新邮箱最后检查时间, ID: {email_id}")
        self._execute(
            "UPDATE emails SET last_check_time = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [email_id],
            commit=True
        )

    def update_email_token(self, email_id, access_token):
        """更新Outlook邮箱的访问令牌"""
        logger.debug(f"更新邮箱访问令牌, ID: {email_id}")
        try:
            encrypted_token = self._encrypt_value(access_token) if access_token else None
            self._execute(
                "UPDATE emails SET access_token = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [encrypted_token, email_id],
                commit=True
            )
            logger.info(f"成功更新邮箱 ID:{email_id} 的访问令牌")
            return True
        except Exception as e:
            logger.error(f"更新邮箱访问令牌失败, ID: {email_id}, 错误: {str(e)}")
            return False

    def delete_email(self, email_id, user_id=None):
        """删除邮箱账号，可以验证所有者"""
        logger.info(f"删除邮箱账号, ID: {email_id}")
        # 构建SQL查询条件
        sql_where = "id = ?"
        params = [email_id]

        if user_id:
            sql_where += " AND user_id = ?"
            params.append(user_id)

        # 先删除相关的邮件记录
        self._execute("DELETE FROM mail_records WHERE email_id = ?", [email_id], commit=True)

        # 再删除邮箱
        self._execute(f"DELETE FROM emails WHERE {sql_where}", params, commit=True)

    def delete_emails(self, email_ids, user_id=None):
        """批量删除邮箱账号，可以验证所有者"""
        if not email_ids:
            return

        logger.info(f"批量删除邮箱账号, IDs: {email_ids}")

        # 如果指定了用户ID，需要验证每个邮箱的所有者
        if user_id:
            # 获取该用户拥有的邮箱
            placeholders = ','.join(['?'] * len(email_ids))
            cursor = self._execute(
                f"SELECT id FROM emails WHERE id IN ({placeholders}) AND user_id = ?",
                email_ids + [user_id]
            )
            valid_ids = [row['id'] for row in cursor.fetchall()]

            if not valid_ids:
                logger.warning(f"用户ID {user_id} 没有权限删除任何指定的邮箱")
                return

            email_ids = valid_ids

        placeholders = ','.join(['?'] * len(email_ids))
        # 先删除相关的邮件记录
        self._execute(f"DELETE FROM mail_records WHERE email_id IN ({placeholders})", email_ids, commit=True)
        # 再删除邮箱
        self._execute(f"DELETE FROM emails WHERE id IN ({placeholders})", email_ids, commit=True)

    def add_mail_record(self, email_id, subject, sender, received_time, content, folder=None, has_attachments=0):
        """添加邮件记录"""
        logger.debug(f"添加邮件记录, 邮箱ID: {email_id}, 主题: {subject}")
        try:
            # 先检查邮件是否已存在
            cursor = self._execute(
                "SELECT id FROM mail_records WHERE email_id = ? AND sender = ? AND subject = ? AND received_time = ?",
                [email_id, sender, subject, received_time]
            )
            exists = cursor.fetchone() is not None

            if exists:
                logger.debug(f"邮件已存在，跳过: 邮箱ID={email_id}, 主题={subject}")
                return False, None  # 邮件已存在，返回False表示没有添加新记录

            # 如果content是字典类型，将其转换为JSON字符串
            if isinstance(content, dict):
                import json
                content = json.dumps(content, ensure_ascii=False)

            has_attachments_value = self._bool_value(has_attachments)

            # 邮件不存在，添加新记录
            self._execute(
                "INSERT INTO mail_records (email_id, subject, sender, received_time, content, folder, has_attachments) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [email_id, subject, sender, received_time, content, folder, has_attachments_value],
                commit=True
            )
            mail_id = self._get_last_insert_id('mail_records')
            return True, mail_id  # 添加了新记录，返回True和邮件ID
        except Exception as e:
            logger.error(f"添加邮件记录失败: {str(e)}")
            return False, None

    def get_mail_records(self, email_id, user_id=None):
        """获取指定邮箱的所有邮件记录，可以验证所有者"""
        logger.debug(f"获取邮箱邮件记录, ID: {email_id}")

        # 如果指定了用户ID，先验证邮箱所有权
        if user_id:
            email = self.get_email_by_id(email_id, user_id)
            if not email:
                logger.warning(f"用户ID {user_id} 没有权限访问邮箱ID {email_id}")
                return []

        cursor = self._execute(
            "SELECT * FROM mail_records WHERE email_id = ? ORDER BY received_time DESC",
            [email_id],
            commit=False
        )

        records = []
        for record in cursor.fetchall():
            # 将记录转换为字典
            record_dict = dict(record)

            # 尝试将content字段从JSON字符串转换为字典
            try:
                if record_dict['content'] and isinstance(record_dict['content'], str):
                    import json
                    try:
                        # 检查是否是JSON格式
                        if record_dict['content'].startswith('{') and record_dict['content'].endswith('}'):
                            record_dict['content'] = json.loads(record_dict['content'])
                    except json.JSONDecodeError:
                        # 如果不是有效的JSON，保持原样
                        pass
            except Exception as e:
                logger.warning(f"解析邮件内容失败: {str(e)}")

            if 'has_attachments' in record_dict and record_dict['has_attachments'] is not None:
                record_dict['has_attachments'] = bool(record_dict['has_attachments'])

            records.append(record_dict)

        return records

    def get_mail_record_by_id(self, mail_id):
        """根据ID获取邮件记录"""
        logger.debug(f"获取邮件记录, ID: {mail_id}")
        try:
            cursor = self._execute(
                "SELECT * FROM mail_records WHERE id = ?",
                [mail_id],
                commit=False
            )
            record = cursor.fetchone()

            if record:
                # 将记录转换为字典
                record_dict = dict(record)

                # 尝试将content字段从JSON字符串转换为字典
                try:
                    if record_dict['content'] and isinstance(record_dict['content'], str):
                        import json
                        try:
                            # 检查是否是JSON格式
                            if record_dict['content'].startswith('{') and record_dict['content'].endswith('}'):
                                record_dict['content'] = json.loads(record_dict['content'])
                        except json.JSONDecodeError:
                            # 如果不是有效的JSON，保持原样
                            pass
                except Exception as e:
                    logger.warning(f"解析邮件内容失败: {str(e)}")

                if 'has_attachments' in record_dict and record_dict['has_attachments'] is not None:
                    record_dict['has_attachments'] = bool(record_dict['has_attachments'])

                return record_dict

            return None
        except Exception as e:
            logger.error(f"获取邮件记录失败: {str(e)}")
            return None

    def add_attachment(self, mail_id, filename, content_type, size, content):
        """添加附件记录"""
        logger.debug(f"添加附件记录, 邮件ID: {mail_id}, 文件名: {filename}")
        try:
            encrypted_content = self._encrypt_bytes(content) if content is not None else None
            self._execute(
                "INSERT INTO attachments (mail_id, filename, content_type, size, content) VALUES (?, ?, ?, ?, ?)",
                [mail_id, filename, content_type, size, encrypted_content],
                commit=True
            )
            attachment_id = self._get_last_insert_id('attachments')

            # 更新邮件记录，标记为有附件
            self._execute(
                "UPDATE mail_records SET has_attachments = ? WHERE id = ?",
                [self._bool_value(True), mail_id],
                commit=True
            )

            return attachment_id
        except Exception as e:
            logger.error(f"添加附件记录失败: {str(e)}")
            return None

    def get_attachments(self, mail_id):
        """获取指定邮件的所有附件信息（不包含内容）"""
        logger.debug(f"获取邮件附件, 邮件ID: {mail_id}")
        try:
            cursor = self._execute(
                "SELECT id, filename, content_type, size, created_at FROM attachments WHERE mail_id = ?",
                [mail_id],
                commit=False
            )
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"获取附件信息失败: {str(e)}")
            return []

    def get_attachment(self, attachment_id):
        """获取指定附件的完整信息（包含内容）"""
        logger.debug(f"获取附件内容, 附件ID: {attachment_id}")
        try:
            cursor = self._execute(
                "SELECT * FROM attachments WHERE id = ?",
                [attachment_id],
                commit=False
            )
            record = self._row_to_dict(cursor.fetchone())
            if record and 'content' in record:
                record['content'] = self._decrypt_bytes(record['content'])
            return record
        except Exception as e:
            logger.error(f"获取附件内容失败: {str(e)}")
            return None

    def search_mail_records(self, email_ids, query, search_in_subject=True, search_in_sender=True, search_in_recipient=False, search_in_content=True):
        """根据条件搜索邮件记录

        Args:
            email_ids: 要搜索的邮箱ID列表
            query: 搜索关键词
            search_in_subject: 是否搜索主题
            search_in_sender: 是否搜索发件人
            search_in_recipient: 是否搜索收件人
            search_in_content: 是否搜索正文内容

        Returns:
            符合条件的邮件记录列表
        """
        if not email_ids or not query:
            return []

        logger.info(f"搜索邮件: 关键词={query}, 邮箱IDs={email_ids}, 范围: 主题={search_in_subject}, 发件人={search_in_sender}, 收件人={search_in_recipient}, 正文={search_in_content}")

        # 准备SQL查询条件
        conditions = []
        params = []

        # 添加邮箱ID条件
        placeholders = ','.join(['?'] * len(email_ids))
        conditions.append(f"email_id IN ({placeholders})")
        params.extend(email_ids)

        # 添加搜索字段条件
        search_conditions = []
        if search_in_subject:
            search_conditions.append("subject LIKE ?")
            params.append(f"%{query}%")

        if search_in_sender:
            search_conditions.append("sender LIKE ?")
            params.append(f"%{query}%")

        if search_in_content:
            search_conditions.append("content LIKE ?")
            params.append(f"%{query}%")

        # 收件人暂时用不到，因为数据库中没有专门的收件人字段
        # 如果需要，可以从邮件内容中解析或在数据库中添加recipient字段

        # 如果没有任何搜索条件，直接返回空列表
        if not search_conditions:
            return []

        # 将所有搜索条件用OR连接
        conditions.append(f"({' OR '.join(search_conditions)})")

        # 构建最终的SQL查询
        sql = f"""
            SELECT mr.*, e.email as recipient
            FROM mail_records mr
            JOIN emails e ON mr.email_id = e.id
            WHERE {' AND '.join(conditions)}
            ORDER BY mr.received_time DESC
        """

        try:
            cursor = self._execute(sql, params, commit=False)
            results = cursor.fetchall()
            logger.info(f"搜索结果: 找到 {len(results)} 条记录")
            return [dict(result) for result in results]
        except Exception as e:
            logger.error(f"搜索邮件记录失败: {str(e)}")
            return []

    def get_emails_by_ids(self, email_ids: List[int]) -> List[Dict]:
        """根据邮箱ID列表获取邮箱信息"""
        try:
            if not email_ids:
                return []

            # 构建IN查询的占位符
            placeholders = ','.join(['?' for _ in email_ids])

            # 执行查询
            cursor = self._execute(f'''
                SELECT id, user_id, email, password, client_id, refresh_token,
                       mail_type, server, port, use_ssl, last_check_time
                FROM emails
                WHERE id IN ({placeholders})
            ''', email_ids, commit=False)

            # 获取结果
            emails = []
            for row in cursor:
                email = self._decrypt_email_record(row)
                emails.append(email)

            return emails

        except Exception as e:
            logger.error(f"获取邮箱信息失败: {str(e)}")
            return []

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            logger.info("关闭数据库连接")
            self.conn.close()
            self.conn = None

    def get_mail_record_by_subject_and_sender(self, email_id, subject, sender):
        """根据主题和发件人获取邮件记录"""
        try:
            cursor = self._execute(
                "SELECT * FROM mail_records WHERE email_id = ? AND subject = ? AND sender = ?",
                [email_id, subject, sender],
                commit=False
            )
            return cursor.fetchone()
        except Exception as e:
            logger.error(f"获取邮件记录失败: {str(e)}")
            return None

    def save_mail_records(self, email_id: int, mail_records: List[Dict], progress_callback: Optional[Callable] = None) -> int:
        """保存邮件记录到数据库"""
        saved_count = 0
        total = len(mail_records)

        logger.info(f"开始保存 {total} 封邮件记录到数据库, 邮箱ID: {email_id}")

        if not progress_callback:
            progress_callback = lambda progress, message: None

        for i, record in enumerate(mail_records):
            try:
                # 更新进度
                progress = int((i + 1) / total * 100)
                progress_message = f"正在保存邮件记录 ({i + 1}/{total})"
                progress_callback(progress, progress_message)

                if i % 10 == 0 or i == total - 1:  # 每10封记录一次进度
                    log_progress(email_id, progress, progress_message)

                # 检查邮件是否已存在
                subject = record.get("subject", "(无主题)")
                sender = record.get("sender", "(未知发件人)")

                logger.debug(f"检查邮件是否存在: '{subject[:30]}...' 发件人: '{sender[:30]}...'")

                existing = self.get_mail_record_by_subject_and_sender(
                    email_id,
                    subject,
                    sender
                )

                if not existing:
                    # 保存新邮件记录
                    logger.debug(f"保存新邮件记录: '{subject[:30]}...'")

                    success = self.add_mail_record(
                        email_id=email_id,
                        subject=subject,
                        sender=sender,
                        content=record.get("content", "(无内容)"),
                        received_time=record.get("received_time", datetime.now()),
                        folder=record.get("folder", "INBOX")
                    )

                    if success:
                        saved_count += 1
                        logger.debug(f"邮件记录保存成功: '{subject[:30]}...'")
                    else:
                        logger.warning(f"邮件记录保存失败: '{subject[:30]}...'")
                else:
                    logger.debug(f"邮件记录已存在: '{subject[:30]}...'")

            except Exception as e:
                logger.error(f"保存邮件记录失败: {str(e)}")
                traceback.print_exc()
                continue

        logger.info(f"完成保存邮件记录: 总计 {total} 封, 新增 {saved_count} 封")
        return saved_count

    def get_all_email_ids(self) -> List[int]:
        """获取所有邮箱的ID列表"""
        try:
            cursor = self._execute("""
                SELECT id FROM emails
            """, commit=False)
            results = cursor.fetchall()
            ids = []
            for row in results:
                if isinstance(row, dict):
                    ids.append(row.get('id'))
                else:
                    ids.append(row[0])
            return ids
        except Exception as e:
            logger.error(f"获取邮箱ID列表失败: {str(e)}")
            return []

    def get_users_with_realtime_check(self) -> List[Dict]:
        """获取启用了实时检查的用户列表"""
        try:
            cursor = self._execute("""
                SELECT id, username, is_admin
                FROM users
                WHERE id IN (
                    SELECT DISTINCT user_id
                    FROM emails
                    WHERE enable_realtime_check = ?
                )
            """, [self._bool_value(True)], commit=False)
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"获取启用实时检查的用户列表失败: {str(e)}")
            return []

    def get_user_emails(self, user_id: int) -> List[Dict]:
        """获取用户的所有邮箱"""
        try:
            cursor = self._execute("""
                SELECT id, email, password, mail_type, server, port,
                       use_ssl, client_id, refresh_token, last_check_time,
                       enable_realtime_check
                FROM emails
                WHERE user_id = ? AND enable_realtime_check = ?
                ORDER BY id
            """, [user_id, self._bool_value(True)], commit=False)
            return [self._decrypt_email_record(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"获取用户邮箱列表失败: {str(e)}")
            return []

    def set_email_realtime_check(self, email_id: int, enable: bool) -> bool:
        """设置邮箱的实时检查状态"""
        try:
            self._execute("""
                UPDATE emails
                SET enable_realtime_check = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, [self._bool_value(enable), email_id], commit=True)
            logger.info(f"已{'启用' if enable else '禁用'}邮箱ID {email_id}的实时检查")
            return True
        except Exception as e:
            logger.error(f"设置邮箱实时检查状态失败: {str(e)}")
            return False
