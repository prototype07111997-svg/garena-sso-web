document.addEventListener('DOMContentLoaded', () => {
    // Inputs & buttons
    const ssoInput = document.getElementById('sso-key-input');
    const usernameInput = document.getElementById('username-input');
    const passwordInput = document.getElementById('password-input');
    
    const convertBtn = document.getElementById('convert-btn');
    const btnSpinner = document.getElementById('btn-spinner');
    const btnText = convertBtn.querySelector('.btn-text');
    
    const successContainer = document.getElementById('success-container');
    const uidVal = document.getElementById('uid-val');
    const usernameVal = document.getElementById('username-val');
    const eventLinkUrl = document.getElementById('event-link-url');
    const copyBtn = document.getElementById('copy-btn');
    const openLinkBtn = document.getElementById('open-link-btn');
    
    const errorContainer = document.getElementById('error-container');
    const errorMsg = document.getElementById('error-msg');
    const toast = document.getElementById('toast');

    // Tabs logic
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabPanes = document.querySelectorAll('.tab-pane');
    let activeTab = 'sso';

    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => b.classList.remove('active'));
            tabPanes.forEach(p => p.classList.remove('active'));
            
            btn.classList.add('active');
            activeTab = btn.getAttribute('data-tab');
            document.getElementById(`tab-${activeTab}`).classList.add('active');
            
            const mainTitle = document.getElementById('main-title');
            if (mainTitle) {
                if (activeTab === 'sso') {
                    mainTitle.innerText = "SSO Key";
                } else {
                    mainTitle.innerText = "Login Tài Khoản tự lấy SSO Key";
                }
            }
            
            successContainer.classList.add('hidden');
            errorContainer.classList.add('hidden');
        });
    });

    // Trigger conversion
    async function handleConvert() {
        let bodyPayload = {};
        
        if (activeTab === 'sso') {
            const ssoKey = ssoInput.value.trim();
            if (!ssoKey) {
                showError('Vui lòng nhập mã Garena SSO Key.');
                return;
            }
            bodyPayload = { sso_key: ssoKey };
        } else {
            const account = usernameInput.value.trim();
            const password = passwordInput.value.trim();
            if (!account || !password) {
                showError('Vui lòng nhập đầy đủ tài khoản và mật khẩu Garena.');
                return;
            }
            bodyPayload = { account, password };
        }

        // Set loading state
        convertBtn.disabled = true;
        ssoInput.disabled = true;
        usernameInput.disabled = true;
        passwordInput.disabled = true;
        btnText.classList.add('hidden');
        btnSpinner.classList.remove('hidden');
        
        successContainer.classList.add('hidden');
        errorContainer.classList.add('hidden');

        try {
            const response = await fetch('/api/convert', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(bodyPayload)
            });

            const result = await response.json();

            if (response.ok && result.status === 'success') {
                showSuccess(result);
            } else {
                let msg = 'Đã xảy ra lỗi khi kết nối tới Garena.';
                const detail = result.detail || '';
                
                if (detail === 'error_session' || detail === 'error_no_session') {
                    msg = 'Mã SSO Key đã hết hạn hoặc không hợp lệ.';
                } else if (detail === 'connection_error') {
                    msg = 'Không thể kết nối đến máy chủ Garena (Lỗi mạng hoặc bị chặn IP).';
                } else if (detail === 'empty_user_info') {
                    msg = 'Không thể lấy thông tin tài khoản từ Garena.';
                } else if (detail === 'INVALID' || detail.includes('result=3') || detail.includes('result=101') || detail.includes('result=105')) {
                    msg = 'Sai tài khoản hoặc mật khẩu Garena.';
                } else if (detail.includes('result=367')) {
                    msg = 'Tài khoản yêu cầu mã xác thực 2 lớp (2FA/OTP). Hãy tắt 2 lớp hoặc sử dụng SSO Key.';
                } else if (detail) {
                    msg = `Lỗi: ${detail}`;
                }
                showError(msg);
            }
        } catch (err) {
            showError('Không thể kết nối tới máy chủ Converter cục bộ.');
        } finally {
            // Reset loading state
            convertBtn.disabled = false;
            ssoInput.disabled = false;
            usernameInput.disabled = false;
            passwordInput.disabled = false;
            btnText.classList.remove('hidden');
            btnSpinner.classList.add('hidden');
        }
    }

    function showSuccess(data) {
        uidVal.textContent = data.result || 'N/A';
        usernameVal.textContent = data.username || 'N/A';
        
        const tokenRow = document.getElementById('token-row');
        const tokenVal = document.getElementById('token-val');
        const qhRow = document.getElementById('qh-row');
        const qhVal = document.getElementById('qh-val');
        
        if (data.tokens) {
            tokenVal.textContent = data.tokens;
            tokenRow.style.display = 'flex';
        } else {
            tokenRow.style.display = 'none';
        }
        
        if (data.qh_msg) {
            qhVal.textContent = data.qh_msg;
            qhRow.style.display = 'flex';
            if (data.qh_msg.includes('thành công') || data.qh_msg.includes('+150')) {
                qhVal.style.color = '#4cd137';
            } else if (data.qh_msg.includes('trước đó')) {
                qhVal.style.color = '#fbc531';
            } else {
                qhVal.style.color = '#e84118';
            }
        } else {
            qhRow.style.display = 'none';
        }
        
        const link = data.event_link || '#';
        eventLinkUrl.textContent = link;
        eventLinkUrl.href = link;
        openLinkBtn.href = link;
        
        successContainer.classList.remove('hidden');
        errorContainer.classList.add('hidden');
    }

    function showError(msg) {
        errorMsg.textContent = msg;
        errorContainer.classList.remove('hidden');
        successContainer.classList.add('hidden');
    }

    // Copy Event Link
    copyBtn.addEventListener('click', () => {
        const textToCopy = eventLinkUrl.textContent;
        if (textToCopy && textToCopy !== '#') {
            navigator.clipboard.writeText(textToCopy).then(() => {
                showToast('Đã sao chép liên kết thành công!');
            }).catch(() => {
                const tempEl = document.createElement('textarea');
                tempEl.value = textToCopy;
                document.body.appendChild(tempEl);
                tempEl.select();
                document.execCommand('copy');
                document.body.removeChild(tempEl);
                showToast('Đã sao chép liên kết thành công!');
            });
        }
    });

    function showToast(message) {
        toast.textContent = message;
        toast.classList.remove('hidden');
        setTimeout(() => {
            toast.classList.add('hidden');
        }, 2000);
    }

    // Donate Modal Logic
    const openDonateBtn = document.getElementById('open-donate-btn');
    const closeDonateBtn = document.getElementById('close-donate-btn');
    const donateModal = document.getElementById('donate-modal');
    const copyStkBtn = document.getElementById('copy-stk-btn');
    const bankStk = document.getElementById('bank-stk');

    if (openDonateBtn && donateModal && closeDonateBtn) {
        openDonateBtn.addEventListener('click', () => {
            donateModal.classList.remove('hidden');
        });

        closeDonateBtn.addEventListener('click', () => {
            donateModal.classList.add('hidden');
        });

        // Close when clicking outside content
        donateModal.addEventListener('click', (e) => {
            if (e.target === donateModal) {
                donateModal.classList.add('hidden');
            }
        });
    }

    if (copyStkBtn && bankStk) {
        copyStkBtn.addEventListener('click', () => {
            const stkText = bankStk.textContent;
            navigator.clipboard.writeText(stkText).then(() => {
                showToast('Đã sao chép số tài khoản thành công!');
            }).catch(() => {
                const tempEl = document.createElement('textarea');
                tempEl.value = stkText;
                document.body.appendChild(tempEl);
                tempEl.select();
                document.execCommand('copy');
                document.body.removeChild(tempEl);
                showToast('Đã sao chép số tài khoản thành công!');
            });
        });
    }

    // Event listeners
    convertBtn.addEventListener('click', handleConvert);
    [ssoInput, usernameInput, passwordInput].forEach(input => {
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                handleConvert();
            }
        });
    });
});
