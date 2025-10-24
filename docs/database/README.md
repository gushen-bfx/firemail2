# 数据库设计与配置文档

## 概述

花火邮箱助手的数据访问层支持三种主流的关系型数据库引擎：SQLite、PostgreSQL 与 MySQL。默认情况下系统仍以 SQLite 的文件数据库作为轻量级依赖，同时也可以通过环境变量切换至外部的 PostgreSQL 或 MySQL 服务。

为了保护敏感数据，项目在数据库读写链路中内置了对称加密：邮箱账号密码、OAuth 凭据和附件内容都会在写入数据库前自动加密，读取时再透明解密。加密密钥采用 Fernet 算法（AES-128 + HMAC）管理，可通过环境变量或本地持久化文件进行配置。

本章节将介绍数据库配置方法，并在后续章节展示核心表结构。有关部署命令示例可参考《docs/部署指南.md》的“环境变量配置”章节。

## 数据库配置

### 1. 选择数据库类型

后端通过 `DB_TYPE` 环境变量决定使用的数据库驱动，取值说明如下：

| 取值 | 说明 | 默认端口 |
| ---- | ---- | -------- |
| `sqlite` | 使用本地 SQLite 文件数据库，位于 `backend/data/huohuo_email.db` | N/A |
| `postgresql` | 连接到 PostgreSQL 服务器 | 5432 |
| `mysql` | 连接到 MySQL / MariaDB 服务器 | 3306 |

若未设置 `DB_TYPE`，系统默认为 `sqlite`。

### 2. 连接字符串配置

可以通过以下两种方式指定 PostgreSQL / MySQL 的连接信息：

1. **完整连接字符串**：设置 `DB_URL` 环境变量，例如：

   ```bash
   export DB_TYPE=postgresql
   export DB_URL=postgresql+psycopg://username:password@db-host:5432/firemail
   ```

2. **分段配置**：分别设置 `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`，系统会自动拼接连接字符串：

   ```bash
   export DB_TYPE=mysql
   export DB_HOST=127.0.0.1
   export DB_PORT=3306
   export DB_NAME=firemail
   export DB_USER=firemail
   export DB_PASSWORD=change_me
   ```

当同时设置了 `DB_URL` 与分段变量时，优先使用 `DB_URL`。

### 3. 加密密钥管理

敏感字段使用 Fernet 对称加密。密钥优先来源于 `DB_ENCRYPTION_KEY` 环境变量，需提供 32 字节的 URL Safe Base64 字符串。如果环境变量未配置，程序会在 `backend/data/db_encryption.key` 中自动生成并持久化密钥文件。

部署在多实例环境时，需要保证各实例使用相同密钥，可将密钥文件挂载为共享卷或统一设置环境变量。

### 4. 连接池与迁移

PostgreSQL/MySQL 驱动默认启用了 SQLAlchemy 的连接池管理，生产环境可根据需求调整 `DB_POOL_SIZE`、`DB_POOL_TIMEOUT` 等高级参数（若未设置则使用默认值）。首次启动时程序会自动创建缺失的表结构，目前无需额外的迁移工具。

## 数据库架构

### 技术选型

- **数据库引擎**：SQLite 3 / PostgreSQL 14+ / MySQL 8+（兼容 MariaDB）
- **存储位置**：
  - SQLite：`backend/data/huohuo_email.db`
  - PostgreSQL/MySQL：由外部数据库服务器维护
- **访问方式**：SQLAlchemy + 对应数据库驱动（SQLite 原生驱动、psycopg、mysqlclient/PyMySQL）
- **连接管理**：统一的数据库会话工厂，支持线程安全访问与连接池复用

### 表结构概述

数据库包含四个主要表：

1. **users**：用户账户信息
2. **emails**：邮箱账户信息
3. **mail_records**：邮件记录信息
4. **system_config**：系统配置信息

## 详细表结构

### 1. users 表

存储用户账户信息，包括用户名、密码和权限等。

#### 表结构：

```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    salt TEXT NOT NULL,
    is_admin INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

#### 字段说明：

| 字段名 | 类型 | 说明 |
|-------|------|------|
| id | INTEGER | 主键，自增 |
| username | TEXT | 用户名，唯一 |
| password | TEXT | 加密后的密码 |
| salt | TEXT | 密码加密的盐值 |
| is_admin | INTEGER | 是否管理员（0否，1是） |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

#### 索引：

- `username` 字段设置了唯一索引

### 2. emails 表

存储邮箱账户信息，与用户表关联。

#### 表结构：

```sql
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
```

#### 字段说明：

| 字段名 | 类型 | 说明 |
|-------|------|------|
| id | INTEGER | 主键，自增 |
| user_id | INTEGER | 外键，关联users表 |
| email | TEXT | 邮箱地址 |
| password | TEXT | 邮箱密码（已加密） |
| mail_type | TEXT | 邮箱类型，默认outlook |
| server | TEXT | 邮件服务器地址（IMAP类型使用） |
| port | INTEGER | 邮件服务器端口（IMAP类型使用） |
| use_ssl | INTEGER | 是否使用SSL（0否，1是） |
| client_id | TEXT | OAuth客户端ID |
| refresh_token | TEXT | OAuth刷新令牌（已加密） |
| access_token | TEXT | OAuth访问令牌（已加密） |
| last_check_time | TIMESTAMP | 上次检查时间 |
| enable_realtime_check | INTEGER | 是否启用实时检查（0否，1是） |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

#### 索引：

- `user_id` 字段设置了外键索引
- `(user_id, email)` 字段组合设置了唯一索引

### 3. mail_records 表

存储邮件记录信息，与邮箱表关联。

#### 表结构：

```sql
CREATE TABLE IF NOT EXISTS mail_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL,
    subject TEXT,
    sender TEXT,
    received_time TIMESTAMP,
    content TEXT,
    folder TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (email_id) REFERENCES emails (id)
)
```

#### 字段说明：

| 字段名 | 类型 | 说明 |
|-------|------|------|
| id | INTEGER | 主键，自增 |
| email_id | INTEGER | 外键，关联emails表 |
| subject | TEXT | 邮件主题 |
| sender | TEXT | 发件人 |
| received_time | TIMESTAMP | 接收时间 |
| content | TEXT | 邮件内容（已加密） |
| folder | TEXT | 邮件文件夹 |
| created_at | TIMESTAMP | 创建时间 |

#### 索引：

- `email_id` 字段设置了外键索引

### 4. system_config 表

存储系统配置信息。

#### 表结构：

```sql
CREATE TABLE IF NOT EXISTS system_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

#### 字段说明：

| 字段名 | 类型 | 说明 |
|-------|------|------|
| id | INTEGER | 主键，自增 |
| key | TEXT | 配置项键名 |
| value | TEXT | 配置项值 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

#### 索引：

- `key` 字段设置了唯一索引

## 敏感数据加密流程

1. 应用初始化时读取（或生成）Fernet 密钥，并创建加解密工具实例。
2. 写入数据库前，对邮箱密码、OAuth 凭据、邮件正文等敏感字段调用 `encrypt_value` 进行加密。
3. 从数据库读取后，对敏感字段调用 `decrypt_value` 还原原始明文数据。

此机制对业务层透明，不需要调用方额外编写加解密逻辑。

## 数据备份

### SQLite 备份

```bash
cp backend/data/huohuo_email.db backup/huohuo_email_$(date +%Y%m%d).db
cp backend/data/db_encryption.key backup/db_encryption_$(date +%Y%m%d).key
```

### PostgreSQL / MySQL 备份

- PostgreSQL：使用 `pg_dump` 导出 `firemail` 数据库，同时妥善保存加密密钥文件/环境变量
- MySQL：使用 `mysqldump` 导出数据库，或通过托管服务的快照功能进行备份

恢复数据时请先恢复数据库，再确保实例使用与备份对应的 `DB_ENCRYPTION_KEY`。

## 常见问题与解决方案

### 1. 并发访问

- SQLite：使用单实例部署并避免长事务，必要时切换至 PostgreSQL/MySQL
- PostgreSQL/MySQL：可通过连接池参数优化高并发访问

### 2. 性能瓶颈

- 定期归档历史邮件记录
- 为常用查询字段添加索引
- 在重负载场景中使用 PostgreSQL/MySQL 并调整硬件资源

### 3. 数据一致性

- 保持数据库事务简短，避免长时间锁表
- 确保多实例部署共享同一加密密钥
- 对外部数据库启用定期备份和监控
