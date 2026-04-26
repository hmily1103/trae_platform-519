/**
 * 平台通用 JavaScript 工具函数
 * 提供统一的错误处理、提示、状态更新等功能
 */

// ==================== 错误提示组件 ====================
class Toast {
    constructor() {
        this.container = null;
        this.init();
    }

    init() {
        // 创建 Toast 容器
        if (!document.getElementById('toast-container')) {
            this.container = document.createElement('div');
            this.container.id = 'toast-container';
            this.container.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                z-index: 9999;
                max-width: 400px;
            `;
            document.body.appendChild(this.container);
        } else {
            this.container = document.getElementById('toast-container');
        }
    }

    show(message, type = 'info', duration = 3000) {
        const toast = document.createElement('div');
        const bgColor = {
            'success': '#28a745',
            'error': '#dc3545',
            'warning': '#ffc107',
            'info': '#17a2b8'
        }[type] || '#17a2b8';

        toast.style.cssText = `
            background-color: ${bgColor};
            color: white;
            padding: 12px 20px;
            margin-bottom: 10px;
            border-radius: 4px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.2);
            animation: slideIn 0.3s ease-out;
            display: flex;
            align-items: center;
            justify-content: space-between;
            min-width: 300px;
        `;

        toast.innerHTML = `
            <span>${this.escapeHtml(message)}</span>
            <button style="background: none; border: none; color: white; font-size: 18px; cursor: pointer; margin-left: 15px; opacity: 0.8;" onclick="this.parentElement.remove()">×</button>
        `;

        this.container.appendChild(toast);

        // 自动移除
        if (duration > 0) {
            setTimeout(() => {
                if (toast.parentElement) {
                    toast.style.animation = 'slideOut 0.3s ease-out';
                    setTimeout(() => toast.remove(), 300);
                }
            }, duration);
        }

        return toast;
    }

    success(message, duration = 3000) {
        return this.show(message, 'success', duration);
    }

    error(message, duration = 5000) {
        return this.show(message, 'error', duration);
    }

    warning(message, duration = 4000) {
        return this.show(message, 'warning', duration);
    }

    info(message, duration = 3000) {
        return this.show(message, 'info', duration);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// 全局 Toast 实例
const toast = new Toast();

// ==================== 错误处理工具 ====================
const ErrorHandler = {
    /**
     * 统一处理 API 错误响应
     */
    handleApiError(error, defaultMessage = '操作失败') {
        console.error('API Error:', error);
        
        let message = defaultMessage;
        
        if (error.response) {
            // HTTP 错误响应
            const data = error.response.data || {};
            message = data.error || data.message || `请求失败 (${error.response.status})`;
        } else if (error.request) {
            // 请求发送但无响应
            message = '服务器无响应，请检查网络连接';
        } else if (error.message) {
            // 其他错误
            message = error.message;
        }

        // 用户友好的错误提示映射
        const friendlyMessages = {
            'Device not connected': '设备未连接，请先连接设备',
            'Device is already running test': '设备正在运行测试，请先停止',
            'no devices configured': '未配置设备，请先添加设备',
            'task already running': '任务正在运行中，请先停止当前任务',
            'Timeout': '操作超时，请重试',
            'Connection refused': '连接被拒绝，请检查设备状态',
            'ECONNREFUSED': '无法连接到设备，请检查 IP 和端口'
        };

        for (const [key, friendly] of Object.entries(friendlyMessages)) {
            if (message.includes(key)) {
                message = friendly;
                break;
            }
        }

        toast.error(message);
        return message;
    },

    /**
     * 处理网络错误
     */
    handleNetworkError() {
        toast.error('网络连接失败，请检查网络设置');
    },

    /**
     * 处理超时错误
     */
    handleTimeoutError() {
        toast.error('操作超时，请重试');
    }
};

// ==================== 加载状态管理 ====================
const LoadingManager = {
    /**
     * 显示加载遮罩
     */
    show(target = document.body, message = '加载中...') {
        const overlay = document.createElement('div');
        overlay.className = 'loading-overlay';
        overlay.style.cssText = `
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(255, 255, 255, 0.8);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            flex-direction: column;
        `;

        overlay.innerHTML = `
            <div class="spinner-border text-primary" role="status" style="width: 3rem; height: 3rem;">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p class="mt-3 text-muted">${message}</p>
        `;

        const container = target.style.position === 'relative' || target === document.body 
            ? target 
            : target.parentElement;
        
        if (container.style.position !== 'relative' && container !== document.body) {
            container.style.position = 'relative';
        }

        overlay.dataset.loadingId = Date.now().toString();
        container.appendChild(overlay);
        return overlay.dataset.loadingId;
    },

    /**
     * 隐藏加载遮罩
     */
    hide(loadingId = null) {
        const overlays = document.querySelectorAll('.loading-overlay');
        if (loadingId) {
            overlays.forEach(overlay => {
                if (overlay.dataset.loadingId === loadingId) {
                    overlay.remove();
                }
            });
        } else {
            overlays.forEach(overlay => overlay.remove());
        }
    }
};

// ==================== 确认对话框 ====================
const ConfirmDialog = {
    /**
     * 显示确认对话框
     */
    show(message, title = '确认操作', options = {}) {
        return new Promise((resolve) => {
            const {
                confirmText = '确认',
                cancelText = '取消',
                confirmClass = 'btn-danger',
                onConfirm = null,
                onCancel = null
            } = options;

            // 创建模态框
            const modal = document.createElement('div');
            modal.className = 'modal fade';
            modal.style.display = 'block';
            modal.setAttribute('tabindex', '-1');
            modal.innerHTML = `
                <div class="modal-dialog">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title">${title}</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                        </div>
                        <div class="modal-body">
                            <p>${message}</p>
                        </div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-secondary" data-cancel>${cancelText}</button>
                            <button type="button" class="btn ${confirmClass}" data-confirm>${confirmText}</button>
                        </div>
                    </div>
                </div>
            `;

            // 添加背景遮罩
            const backdrop = document.createElement('div');
            backdrop.className = 'modal-backdrop fade show';
            document.body.appendChild(backdrop);
            document.body.appendChild(modal);

            // 确保显示
            setTimeout(() => {
                modal.classList.add('show');
            }, 10);

            // 清理函数
            function cleanup() {
                // 修复：强制移除 Bootstrap 添加的 body 类和样式，防止页面无法滚动
                document.body.classList.remove('modal-open');
                document.body.style.overflow = '';
                document.body.style.paddingRight = '';

                if (modal && modal.parentElement) {
                    modal.remove();
                }
                if (backdrop && backdrop.parentElement) {
                    backdrop.remove();
                }
                // 移除所有可能残留的遮罩
                const remainingBackdrops = document.querySelectorAll('.modal-backdrop');
                remainingBackdrops.forEach(b => b.remove());
            }

            // 绑定事件
            modal.querySelector('[data-confirm]').addEventListener('click', () => {
                cleanup();
                if (onConfirm) onConfirm();
                resolve(true);
            });

            modal.querySelector('[data-cancel]').addEventListener('click', () => {
                cleanup();
                if (onCancel) onCancel();
                resolve(false);
            });

            // 关闭按钮事件
            const closeBtn = modal.querySelector('.btn-close');
            if (closeBtn) {
                closeBtn.addEventListener('click', () => {
                    cleanup();
                    resolve(false);
                });
            }
        });
    }
};

// ==================== 状态更新管理器 ====================
class StatusUpdater {
    constructor(callback, interval = 2000) {
        this.callback = callback;
        this.interval = interval;
        this.timer = null;
        this.isRunning = false;
    }

    static create(callback, interval) {
        return new StatusUpdater(callback, interval);
    }

    start() {
        if (this.isRunning) return;
        this.isRunning = true;
        
        // 立即执行一次
        if (this.callback) {
            try {
                this.callback();
            } catch (e) {
                console.error('Initial callback failed:', e);
            }
        }
        
        this.timer = setInterval(async () => {
            if (this.isRunning && this.callback) {
                try {
                    await this.callback();
                } catch (e) {
                    console.error('Status update callback failed:', e);
                }
            }
        }, this.interval);
        
        console.log(`StatusUpdater started (interval: ${this.interval}ms)`);
    }

    stop() {
        this.isRunning = false;
        if (this.timer) {
            clearInterval(this.timer);
            this.timer = null;
        }
        console.log('StatusUpdater stopped');
    }
}

// ==================== API Client ====================
const ApiClient = {
    request(method, url, data = null, options = {}) {
        const {
            showLoading = true,
            loadingMessage = '处理中...',
            showError = true,
            showSuccess = false,
            successMessage = '操作成功',
            timeout = 30000 // 默认超时时间
        } = options;

        let loadingId = null;
        if (showLoading) {
            loadingId = LoadingManager.show(document.body, loadingMessage);
        }

        return new Promise((resolve, reject) => {
            $.ajax({
                url: url,
                type: method,
                contentType: 'application/json',
                data: data ? JSON.stringify(data) : null,
                timeout: timeout,
                success: (response) => {
                    if (showLoading) LoadingManager.hide(loadingId);
                    
                    if (response && (response.success || response.ok)) {
                        if (showSuccess) toast.success(successMessage);
                        resolve(response);
                    } else {
                        const msg = (response && response.message) || '操作失败';
                        if (showError) toast.error(msg);
                        reject(new Error(msg));
                    }
                },
                error: (xhr, status, error) => {
                    if (showLoading) LoadingManager.hide(loadingId);
                    
                    let errorMsg = '请求失败';
                    if (status === 'timeout') {
                        errorMsg = '请求超时，请重试';
                    } else if (xhr.responseJSON && xhr.responseJSON.message) {
                        errorMsg = xhr.responseJSON.message;
                    } else if (xhr.statusText) {
                        errorMsg = xhr.statusText;
                    }
                    
                    if (showError) toast.error(errorMsg);
                    reject(new Error(errorMsg));
                }
            });
        });
    },

    get(url, options = {}) {
        return this.request('GET', url, null, options);
    },

    post(url, data, options = {}) {
        return this.request('POST', url, data, options);
    },

    put(url, data, options = {}) {
        return this.request('PUT', url, data, options);
    },

    delete(url, options = {}) {
        return this.request('DELETE', url, null, options);
    }
};

// 暴露到全局，供各页面使用
window.toast = toast;
window.LoadingManager = LoadingManager;
window.ErrorHandler = ErrorHandler;
window.ApiClient = ApiClient;

// ==================== 表单校验 ====================
const FormValidator = {
    validateRequired(form, fields, options = {}) {
        const { showToast = true } = options;
        for (const name of fields) {
            const el = form.querySelector(`[name="${name}"]`) || form.querySelector(`#${name}`);
            const val = el ? (el.value || '').trim() : '';
            if (!val) {
                const label = el && el.closest('.mb-2') ? el.closest('.mb-2').querySelector('label') : null;
                const msg = (label && label.textContent) ? `请填写 ${label.textContent.trim()}` : `请填写 ${name}`;
                if (showToast && window.toast) toast.warning(msg);
                if (el) el.focus();
                return false;
            }
        }
        return true;
    }
};
window.FormValidator = FormValidator;
