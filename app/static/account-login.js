// Both login entrypoints share this transient dialog; nothing is stored locally.
window.promptAccountCredentials = function () {
  return new Promise(resolve => {
    const dialog = document.createElement('dialog');
    dialog.className = 'credential-dialog';
    dialog.setAttribute('aria-labelledby', 'credential-title');
    dialog.innerHTML = `<form autocomplete="off">
      <h2 id="credential-title">账号密码登录</h2>
      <label for="credential-input">账号、密码和 2FA 密钥</label>
      <input id="credential-input" type="password" autocomplete="off" spellcheck="false" maxlength="8192" required aria-describedby="credential-format credential-notice" placeholder="粘贴一行账号信息">
      <p id="credential-format" class="hint">格式：邮箱----密码----2FA密钥，也支持用 --- 分隔。</p>
      <p id="credential-notice" class="hint">2FA 密钥会发送给第三方网站 2fa.fun 获取验证码。密码和密钥仅用于本次登录；如需额外验证，请在登录窗口完成。</p>
      <p class="error" role="alert" hidden></p>
      <div class="toolbar"><button type="button">取消</button><button type="submit" class="primary">登录</button></div>
    </form>`;
    const input = dialog.querySelector('input');
    const error = dialog.querySelector('[role="alert"]');
    const discard = () => dialog.close();
    dialog
      .querySelector('button[type="button"]')
      .addEventListener('click', discard);
    dialog.querySelector('form').addEventListener('submit', event => {
      event.preventDefault();
      const value = input.value.trim();
      if (
        !/^[^\s@]+@[^\s@]+?-{3,}.+-{3,}[a-z2-7= ]{16,}$/i.test(value) ||
        /[\r\n]/.test(value)
      ) {
        error.textContent = '请按格式填写邮箱、密码和 2FA 密钥，每次一行。';
        error.hidden = false;
        input.focus();
        return;
      }
      dialog.close('submit');
    });
    dialog.addEventListener(
      'close',
      () => {
        const result =
          dialog.returnValue === 'submit' ? input.value.trim() : null;
        input.value = '';
        dialog.remove();
        window.removeEventListener('pagehide', discard);
        resolve(result);
      },
      { once: true }
    );
    window.addEventListener('pagehide', discard, { once: true });
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
};
