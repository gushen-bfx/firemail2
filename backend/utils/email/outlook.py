"""Outlook 邮箱处理模块。"""

from __future__ import annotations

import email
import imaplib
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Optional, Tuple

import requests

from .common import (
    decode_mime_words,
    format_date_for_imap_search,
    normalize_check_time,
)
from .logger import logger


class OutlookOAuthError(RuntimeError):
    """在与 Microsoft OAuth 服务交互时抛出的异常。"""


@dataclass
class OutlookToken:
    """Outlook OAuth2 访问令牌信息。"""

    access_token: str
    refresh_token: Optional[str] = None


class OutlookMailHandler:
    """封装与 Outlook/Hotmail 邮箱交互的逻辑。"""

    DEFAULT_FOLDERS = {
        'INBOX': ['inbox', 'Inbox', 'INBOX'],
        'SENT': ['sentitems', 'Sent Items', 'Sent', '已发送'],
        'DRAFTS': ['drafts', 'Drafts', '草稿箱'],
        'TRASH': ['deleteditems', 'Deleted Items', 'Trash', '已删除'],
        'SPAM': ['junkemail', 'Junk E-mail', 'Spam', '垃圾邮件'],
        'ARCHIVE': ['archive', 'Archive', '归档'],
    }

    IMAP_HOSTS: Tuple[str, ...] = (
        'outlook.office365.com',
        'outlook.office.com',
        'imap-mail.outlook.com',
    )
    TOKEN_ENDPOINT_TEMPLATE = 'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token'
    DEFAULT_SCOPE = 'https://outlook.office365.com/.default offline_access'

    def __init__(self, email_address: str, access_token: str, *, preferred_host: Optional[str] = None):
        """初始化 Outlook 处理器。

        Args:
            email_address: 邮箱地址。
            access_token: OAuth2 访问令牌。
            preferred_host: 优先尝试的 IMAP 主机。
        """

        self.email_address = email_address
        self.access_token = access_token
        self.mail: Optional[imaplib.IMAP4_SSL] = None
        self.error: Optional[str] = None
        self._preferred_host = preferred_host

    # ------------------------------------------------------------------
    # 连接与会话管理
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """连接到 Outlook IMAP 服务器。"""

        hosts = self._iter_hosts()
        last_error: Optional[Exception] = None

        for host in hosts:
            try:
                logger.debug("尝试使用主机 %s 连接 Outlook IMAP", host)
                connection = imaplib.IMAP4_SSL(host)
                auth_bytes = self.generate_auth_bytes(self.email_address, self.access_token)
                connection.authenticate('XOAUTH2', lambda _: auth_bytes)
                self.mail = connection
                self.error = None
                logger.info("成功连接到 Outlook IMAP 主机 %s", host)
                return True
            except Exception as exc:  # pragma: no cover - 网络/环境依赖
                last_error = exc
                logger.error("Outlook 连接失败 (%s): %s", host, exc)

        if last_error:
            self.error = str(last_error)
        return False

    def _iter_hosts(self) -> Iterator[str]:
        """返回首选主机优先的迭代器。"""

        yield from self._iter_hosts_static(self._preferred_host)

    @classmethod
    def _iter_hosts_static(cls, preferred_host: Optional[str] = None) -> Iterable[str]:
        """返回 Outlook IMAP 主机列表，优先返回首选主机。"""

        if preferred_host:
            normalized = preferred_host.strip()
            if normalized:
                yield normalized

        for host in cls.IMAP_HOSTS:
            if preferred_host and host.strip().lower() == preferred_host.strip().lower():
                continue
            yield host

    def get_folders(self):
        """获取文件夹列表"""
        if not self.mail:
            return []

        try:
            _, folders = self.mail.list()
            folder_list = []

            for folder in folders:
                if isinstance(folder, bytes):
                    folder = folder.decode('utf-8', errors='ignore')

                # 解析文件夹名称
                parts = folder.split('"')
                if len(parts) >= 3:
                    folder_name = parts[-2]
                else:
                    folder_name = folder.split()[-1]

                if folder_name and folder_name not in ['.', '..']:
                    folder_list.append(folder_name)

            # 确保常用文件夹在列表中
            default_folders = ['inbox', 'sentitems', 'drafts', 'deleteditems', 'junkemail']
            for df in default_folders:
                if df not in folder_list:
                    folder_list.append(df)

            return sorted(folder_list)
        except Exception as e:
            logger.error(f"获取Outlook文件夹列表失败: {e}")
            return ['inbox']

    def get_messages(self, folder="inbox", limit=100):
        """获取指定文件夹的邮件"""
        if not self.mail:
            return []

        try:
            self.mail.select(folder)
            _, messages = self.mail.search(None, 'ALL')
            message_numbers = messages[0].split()

            # 限制数量并倒序（最新的在前）
            message_numbers = message_numbers[-limit:] if len(message_numbers) > limit else message_numbers
            message_numbers.reverse()

            mail_list = []
            for num in message_numbers:
                try:
                    _, msg_data = self.mail.fetch(num, '(RFC822)')
                    email_body = msg_data[0][1]
                    msg = email.message_from_bytes(email_body)

                    # 简化的邮件解析
                    subject = decode_mime_words(msg.get('Subject', ''))
                    sender = decode_mime_words(msg.get('From', ''))
                    received_time = email.utils.parsedate_to_datetime(msg.get('Date', ''))

                    # 获取邮件内容
                    content = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type in ['text/plain', 'text/html']:
                                try:
                                    part_content = part.get_payload(decode=True).decode()
                                    content += part_content
                                except:
                                    pass
                    else:
                        try:
                            content = msg.get_payload(decode=True).decode()
                        except:
                            content = str(msg.get_payload())

                    mail_list.append({
                        'subject': subject,
                        'sender': sender,
                        'received_time': received_time,
                        'content': content,
                        'folder': folder
                    })
                except Exception as e:
                    logger.warning(f"解析Outlook邮件失败: {e}")
                    continue

            return mail_list
        except Exception as e:
            logger.error(f"获取Outlook邮件失败: {e}")
            return []

    def close(self):
        """关闭连接。"""

        if not self.mail:
            return

        try:
            self.mail.logout()
        except Exception:  # pragma: no cover - 依赖外部服务器
            pass
        finally:
            self.mail = None

    # ------------------------------------------------------------------
    # OAuth 令牌
    # ------------------------------------------------------------------
    @staticmethod
    def acquire_token(
        refresh_token: str,
        client_id: str,
        *,
        tenant_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout: int = 15,
    ) -> OutlookToken:
        """使用刷新令牌换取新的访问令牌。"""

        tenant = (tenant_id or 'common').strip() or 'common'
        url = OutlookMailHandler.TOKEN_ENDPOINT_TEMPLATE.format(tenant=tenant)
        data = {
            'client_id': client_id,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
        }

        if client_secret:
            data['client_secret'] = client_secret
        else:
            data['scope'] = OutlookMailHandler.DEFAULT_SCOPE

        try:
            response = requests.post(url, data=data, timeout=timeout)
        except requests.RequestException as exc:  # pragma: no cover - 网络异常
            logger.error("请求 Outlook 令牌失败: %s", exc)
            raise OutlookOAuthError(f"请求令牌失败: {exc}") from exc

        payload = {}
        try:
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # pragma: no cover - 第三方响应异常
            logger.error("解析 Outlook 令牌响应失败: %s", exc)
            raise OutlookOAuthError("无法解析访问令牌响应") from exc

        access_token = payload.get('access_token')
        if not access_token:
            error_description = payload.get('error_description') or payload.get('error')
            logger.error("获取访问令牌失败: %s", error_description)
            raise OutlookOAuthError(error_description or '无法获取访问令牌，请检查凭据')

        refresh = payload.get('refresh_token')
        if refresh and refresh != refresh_token:
            logger.info("Outlook 返回了新的刷新令牌")

        logger.info("成功获取新的访问令牌")
        return OutlookToken(access_token=access_token, refresh_token=refresh)

    @staticmethod
    def generate_auth_bytes(user: str, token: str) -> bytes:
        """生成 XOAUTH2 授权字节串。"""

        return f"user={user}\1auth=Bearer {token}\1\1".encode('utf-8')

    # ------------------------------------------------------------------
    # 邮件同步
    # ------------------------------------------------------------------
    @staticmethod
    def fetch_emails(
        email_address,
        access_token,
        folder="inbox",
        callback: Optional[Callable[[int, str], None]] = None,
        last_check_time=None,
    ):
        """
        通过IMAP协议获取Outlook/Hotmail邮箱中的邮件

        Args:
            email_address: 邮箱地址
            access_token: OAuth2访问令牌
            folder: 邮件文件夹，默认为收件箱
            callback: 进度回调函数
            last_check_time: 上次检查时间，如果提供，只获取该时间之后的邮件

        Returns:
            list: 邮件记录列表
        """
        mail_records = []

        # 确保回调函数存在
        if callback is None:
            callback = lambda progress, folder: None

        # 标准化处理last_check_time
        last_check_time = normalize_check_time(last_check_time)

        # 日志记录
        if last_check_time:
            logger.info(f"获取Outlook邮箱{email_address}中{folder}文件夹自{last_check_time.isoformat()}以来的新邮件")
        else:
            logger.info(f"获取Outlook邮箱{email_address}中{folder}文件夹的所有邮件")

        # 尝试连接次数
        max_retries = 3

        for retry in range(max_retries):
            try:
                logger.info(f"尝试连接Outlook邮箱 (尝试 {retry+1}/{max_retries})")
                callback(10, folder)

                # 创建IMAP连接
                hosts = OutlookMailHandler._iter_hosts_static()
                last_exc: Optional[Exception] = None
                mail = None

                for host in hosts:
                    try:
                        logger.debug("连接 Outlook 主机 %s", host)
                        mail = imaplib.IMAP4_SSL(host)
                        auth_bytes = OutlookMailHandler.generate_auth_bytes(email_address, access_token)
                        mail.authenticate('XOAUTH2', lambda _: auth_bytes)
                        logger.debug("Outlook 主机 %s 认证成功", host)
                        break
                    except Exception as host_exc:  # pragma: no cover - 网络依赖
                        last_exc = host_exc
                        logger.warning("连接 Outlook 主机 %s 失败: %s", host, host_exc)
                        if mail is not None:
                            try:
                                mail.logout()
                            except Exception:
                                pass
                            mail = None

                if mail is None:
                    raise last_exc or RuntimeError('无法连接任何 Outlook IMAP 主机')

                # 选择文件夹
                target_folder = folder or 'inbox'
                mail.select(target_folder)
                callback(20, folder)

                # 定义搜索条件
                if last_check_time:
                    # 将上次检查时间转换为IMAP日期格式 (DD-MMM-YYYY)
                    search_date = format_date_for_imap_search(last_check_time)
                    search_cmd = f'(SINCE "{search_date}")'
                    logger.info(f"搜索{search_date}之后的邮件")
                    status, data = mail.search(None, search_cmd)
                else:
                    # 获取最近的100封邮件
                    status, data = mail.search(None, 'ALL')

                if status != 'OK':
                    logger.error(f"搜索邮件失败: {status}")
                    continue

                # 获取所有邮件ID
                mail_ids = data[0].split()

                # 只处理最近的100封邮件
                mail_ids = mail_ids[-100:] if len(mail_ids) > 100 else mail_ids

                total_mails = len(mail_ids)
                logger.info(f"找到{total_mails}封邮件")

                # 处理每封邮件
                for i, mail_id in enumerate(mail_ids):
                    # 更新进度
                    progress = int(20 + (i / total_mails) * 70) if total_mails > 0 else 90
                    callback(progress, folder)

                    try:
                        # 获取邮件
                        status, mail_data = mail.fetch(mail_id, '(RFC822)')

                        if status != 'OK':
                            logger.error(f"获取邮件ID {mail_id} 失败: {status}")
                            continue

                        # 解析邮件
                        msg = email.message_from_bytes(mail_data[0][1])

                        # 获取邮件基本信息
                        subject = decode_mime_words(msg.get('Subject', ''))
                        sender = decode_mime_words(msg.get('From', ''))
                        received_time = email.utils.parsedate_to_datetime(msg.get('Date', ''))

                        # 创建唯一标识，用于去重
                        mail_key = f"{subject}|{sender}|{received_time.isoformat() if received_time else 'unknown'}"

                        # 检查此邮件是否已处理（通过内存中的集合进行快速检查）
                        if mail_key in [record.get('mail_key') for record in mail_records]:
                            logger.info(f"跳过重复邮件: {subject}")
                            continue

                        # 获取邮件内容
                        content = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                if content_type == 'text/plain' or content_type == 'text/html':
                                    try:
                                        part_content = part.get_payload(decode=True).decode()
                                        content += part_content
                                    except:
                                        pass
                        else:
                            content = msg.get_payload(decode=True).decode()

                        # 添加到结果列表
                        mail_records.append({
                            'subject': subject,
                            'sender': sender,
                            'received_time': received_time,
                            'content': content,
                            'mail_key': mail_key  # 添加唯一标识，用于后续去重
                        })

                    except Exception as e:
                        logger.error(f"处理邮件ID {mail_id} 时出错: {str(e)}")

                # 成功获取邮件，跳出重试循环
                callback(90, folder)
                break

            except imaplib.IMAP4.error as e:
                logger.error(f"IMAP错误: {str(e)}")
                time.sleep(1)  # 等待一秒再重试

            except Exception as e:
                logger.error(f"获取邮件异常: {str(e)}")
                time.sleep(1)  # 等待一秒再重试

            finally:
                # 确保关闭连接
                try:
                    if mail is not None:
                        mail.logout()
                except Exception:  # pragma: no cover - 依赖外部服务器
                    pass

        return mail_records

    @staticmethod
    def check_mail(email_info, db, progress_callback=None):
        """检查Outlook/Hotmail邮箱中的邮件并存储到数据库"""
        email_id = email_info['id']
        email_address = email_info['email']
        refresh_token = email_info['refresh_token']
        client_id = email_info['client_id']
        tenant_id = email_info.get('tenant_id')
        client_secret = email_info.get('client_secret')

        logger.info(f"开始检查Outlook邮箱: ID={email_id}, 邮箱={email_address}")

        # 确保回调函数存在
        if progress_callback is None:
            progress_callback = lambda progress, message: None

        # 报告初始进度
        progress_callback(0, "正在获取访问令牌...")

        try:
            # 获取新的访问令牌
            try:
                token = OutlookMailHandler.acquire_token(
                    refresh_token,
                    client_id,
                    tenant_id=tenant_id,
                    client_secret=client_secret,
                )
            except OutlookOAuthError as oauth_error:
                error_msg = f"邮箱{email_address}(ID={email_id})获取访问令牌失败: {oauth_error}"
                logger.error(error_msg)
                progress_callback(0, str(oauth_error))
                return {
                    'success': False,
                    'message': str(oauth_error),
                }

            access_token = token.access_token

            # 更新令牌到数据库
            db.update_email_tokens(email_id, access_token, token.refresh_token)

            # 报告进度
            progress_callback(10, "开始获取邮件...")

            # 获取邮件
            def folder_progress_callback(progress, folder):
                msg = f"正在处理{folder}文件夹，进度{progress}%"
                # 将各文件夹的进度映射到总进度10-90%
                total_progress = 10 + int(progress * 0.8)
                progress_callback(total_progress, msg)

            try:
                mail_records = OutlookMailHandler.fetch_emails(
                    email_address,
                    access_token,
                    "inbox",
                    folder_progress_callback,
                )

                # 报告进度
                count = len(mail_records)
                progress_callback(90, f"获取到{count}封邮件，正在保存...")

                # 将邮件记录保存到数据库
                saved_count = 0
                for record in mail_records:
                    try:
                        success = db.add_mail_record(
                            email_id,
                            record['subject'],
                            record['sender'],
                            record['received_time'],
                            record['content']
                        )
                        if success:
                            saved_count += 1
                    except Exception as e:
                        logger.error(f"保存邮件记录失败: {str(e)}")

                # 更新最后检查时间（只有在成功获取到邮件或没有新邮件时才更新）
                try:
                    if count >= 0:  # 确保邮件获取成功
                        db.update_check_time(email_id)
                        logger.info(f"已更新邮箱{email_address}(ID={email_id})的最后检查时间")
                    else:
                        logger.warning(f"邮件获取失败，不更新邮箱{email_address}(ID={email_id})的最后检查时间")
                except Exception as e:
                    logger.error(f"更新检查时间失败: {str(e)}")

                # 报告完成
                success_msg = f"完成，共处理{count}封邮件，新增{saved_count}封"
                progress_callback(100, success_msg)

                logger.info(f"邮箱{email_address}(ID={email_id})检查完成，获取到{count}封邮件，新增{saved_count}封")
                return {
                    'success': True,
                    'message': success_msg,
                    'total': count,
                    'saved': saved_count
                }

            except Exception as e:
                error_msg = f"检查邮件失败: {str(e)}"
                logger.error(f"邮箱{email_address}(ID={email_id}){error_msg}")
                progress_callback(0, error_msg)
                return {
                    'success': False,
                    'message': error_msg
                }

        except Exception as e:
            error_msg = f"处理邮箱过程中出错: {str(e)}"
            logger.error(f"邮箱{email_address}(ID={email_id}){error_msg}")
            progress_callback(0, error_msg)
            return {
                'success': False,
                'message': error_msg
            }
