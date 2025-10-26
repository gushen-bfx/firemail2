import logging
from typing import Optional, Tuple

from .imap import IMAPMailHandler
from .outlook import OutlookMailHandler, OutlookOAuthError

logger = logging.getLogger(__name__)


def verify_email_credentials(
    mail_type: str,
    email_address: str,
    password: Optional[str] = None,
    client_id: Optional[str] = None,
    refresh_token: Optional[str] = None,
    server: Optional[str] = None,
    port: Optional[int] = None,
    use_ssl: bool = True,
    client_secret: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """验证邮箱配置是否有效。

    Returns:
        (是否验证通过, 提示信息)
    """

    normalized_type = (mail_type or 'imap').lower()
    try:
        if normalized_type == 'outlook':
            if not client_id or not refresh_token:
                return False, 'Outlook邮箱需要提供Client ID和Refresh Token'

            normalized_tenant = (tenant_id or 'common').strip() or 'common'
            normalized_secret = client_secret.strip() if isinstance(client_secret, str) else client_secret

            try:
                token = OutlookMailHandler.acquire_token(
                    refresh_token,
                    client_id,
                    tenant_id=normalized_tenant,
                    client_secret=normalized_secret,
                )
            except OutlookOAuthError as exc:
                return False, f'无法获取访问令牌: {exc}'

            handler = OutlookMailHandler(email_address, token.access_token)
            if not handler.connect():
                error_message = handler.error or '无法连接到Outlook服务器'
                return False, error_message

            handler.close()
            return True, '邮箱连接验证成功'

        # 统一处理IMAP类邮箱
        if not password:
            return False, '邮箱密码不能为空'

        if normalized_type == 'gmail':
            server = 'imap.gmail.com'
            port = 993
            use_ssl = True
        elif normalized_type == 'qq':
            server = 'imap.qq.com'
            port = 993
            use_ssl = True

        handler = IMAPMailHandler(server, email_address, password, use_ssl=use_ssl, port=port)
        if handler.connect():
            handler.close()
            return True, '邮箱连接验证成功'

        error_message = handler.error or '无法连接到邮箱服务器，请检查配置'
        return False, error_message

    except Exception as exc:  # pragma: no cover - 运行时异常记录
        logger.error(f'验证邮箱凭据失败: {exc}', exc_info=True)
        return False, f'验证邮箱凭据失败: {exc}'
