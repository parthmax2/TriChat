class TriChat {
    constructor() {
        this.ws = null;
        this.username = "";
        this.room = "global";
        this.isConnected = false;
        this.users = [];

        this.initElements();
        this.bindEvents();
        this.setupMessageInputResize();
        this.updateFieldStates();
        this.showWelcomeMessage();
    }

    initElements() {
        this.elements = {
            appShell: document.getElementById("appShell"),
            connectScreen: document.getElementById("connectScreen"),
            chatScreen: document.getElementById("chatScreen"),
            connectionForm: document.getElementById("connectionForm"),
            messageForm: document.getElementById("messageForm"),
            usernameInput: document.getElementById("usernameInput"),
            roomInput: document.getElementById("roomInput"),
            connectBtn: document.getElementById("connectBtn"),
            disconnectBtn: document.getElementById("disconnectBtn"),
            mobileBackBtn: document.getElementById("mobileBackBtn"),
            status: document.getElementById("status"),
            roomTitle: document.getElementById("roomTitle"),
            onlineCount: document.getElementById("onlineCount"),
            messages: document.getElementById("messages"),
            messageInput: document.getElementById("messageInput"),
            sendBtn: document.getElementById("sendBtn"),
            fileInput: document.getElementById("fileInput"),
            userList: document.getElementById("userList"),
            emptyUsers: document.getElementById("emptyUsers"),
            usersToggle: document.getElementById("usersToggle"),
            usersClose: document.getElementById("usersClose"),
            drawerBackdrop: document.getElementById("drawerBackdrop")
        };
    }

    bindEvents() {
        this.elements.connectionForm.addEventListener("submit", (event) => {
            event.preventDefault();
            this.connect();
        });

        this.elements.messageForm.addEventListener("submit", (event) => {
            event.preventDefault();
            this.sendMessage();
        });

        this.elements.disconnectBtn.addEventListener("click", () => this.disconnect());
        this.elements.mobileBackBtn.addEventListener("click", () => this.disconnect());
        this.elements.fileInput.addEventListener("change", (event) => this.handleFileSelect(event));
        this.elements.usersToggle.addEventListener("click", () => this.toggleUsersPanel());
        this.elements.usersClose.addEventListener("click", () => this.closeUsersPanel());
        this.elements.drawerBackdrop.addEventListener("click", () => this.closeUsersPanel());

        this.elements.messageInput.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                this.sendMessage();
            }
        });

        this.elements.usernameInput.addEventListener("input", () => this.updateFieldStates());
        this.elements.roomInput.addEventListener("input", () => this.updateFieldStates());

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") this.closeUsersPanel();
        });
    }

    setupMessageInputResize() {
        this.elements.messageInput.addEventListener("input", () => {
            this.elements.messageInput.style.height = "auto";
            this.elements.messageInput.style.height = `${Math.min(this.elements.messageInput.scrollHeight, 136)}px`;
        });
    }

    updateFieldStates() {
        const usernameShell = this.elements.usernameInput.closest(".field-shell");
        const roomShell = this.elements.roomInput.closest(".field-shell");

        usernameShell.classList.toggle("has-value", Boolean(this.elements.usernameInput.value.trim()));
        roomShell.classList.toggle("has-value", Boolean(this.elements.roomInput.value.trim()));
    }

    async connect() {
        const username = this.elements.usernameInput.value.trim();
        const room = this.elements.roomInput.value.trim() || "global";

        if (!username) {
            this.updateStatus("Please enter your name", "disconnected");
            this.elements.usernameInput.focus();
            return;
        }

        this.username = username;
        this.room = room;
        this.elements.roomTitle.textContent = room;

        try {
            this.updateStatus("Connecting...", "neutral");
            this.elements.connectBtn.disabled = true;

            const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
            const wsUrl = `${protocol}//${window.location.host}/ws/${encodeURIComponent(room)}?username=${encodeURIComponent(username)}`;

            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                this.isConnected = true;
                this.elements.connectBtn.disabled = false;
                this.updateStatus(`Connected to ${room}`, "connected");
                this.updateUI();
                this.elements.messageInput.focus();
            };

            this.ws.onmessage = (event) => {
                let message;
                try {
                    message = JSON.parse(event.data);
                } catch (error) {
                    console.error("Invalid WebSocket message:", error);
                    return;
                }

                if (message.type === "user_list") {
                    this.updateUserList(message.users || []);
                    return;
                }

                this.displayMessage(message);
            };

            this.ws.onclose = () => {
                this.isConnected = false;
                this.elements.connectBtn.disabled = false;
                this.updateStatus("Disconnected", "disconnected");
                this.updateUI();
            };

            this.ws.onerror = (error) => {
                console.error("WebSocket error:", error);
                this.elements.connectBtn.disabled = false;
                this.updateStatus("Connection error", "disconnected");
            };
        } catch (error) {
            console.error("Connection failed:", error);
            this.elements.connectBtn.disabled = false;
            this.updateStatus("Failed to connect", "disconnected");
        }
    }

    disconnect() {
        if (this.ws) {
            this.ws.close();
        }
    }

    updateUI() {
        document.body.classList.remove("users-open");
        this.elements.usersToggle.setAttribute("aria-expanded", "false");

        if (this.isConnected) {
            this.elements.appShell.classList.add("is-chatting");
            this.elements.connectScreen.classList.add("is-hidden");
            this.elements.chatScreen.classList.remove("is-hidden");
            this.elements.chatScreen.classList.add("is-connected-preview");
            this.elements.usernameInput.disabled = true;
            this.elements.roomInput.disabled = true;
        } else {
            this.elements.appShell.classList.remove("is-chatting");
            this.elements.connectScreen.classList.remove("is-hidden");
            this.elements.chatScreen.classList.add("is-hidden");
            this.elements.chatScreen.classList.remove("is-connected-preview");
            this.elements.usernameInput.disabled = false;
            this.elements.roomInput.disabled = false;
            this.elements.messages.innerHTML = "";
            this.users = [];
            this.updateUserList([]);
            this.showWelcomeMessage();
        }
    }

    updateStatus(text, type) {
        const icon = type === "connected" ? "fa-check-circle" : type === "disconnected" ? "fa-times-circle" : "fa-info-circle";
        this.elements.status.innerHTML = `<i class="fas ${icon}"></i><span>${this.escapeHtml(text)}</span>`;
        this.elements.status.className = `status ${type}`;
    }

    updateUserList(users) {
        this.users = users;
        this.elements.userList.innerHTML = "";

        users.forEach((user) => {
            const li = document.createElement("li");
            li.textContent = user;
            this.elements.userList.appendChild(li);
        });

        const count = users.length;
        this.elements.onlineCount.textContent = `${count} ${count === 1 ? "member" : "members"} online`;
        this.elements.emptyUsers.classList.toggle("is-hidden", count > 0);
    }

    sendMessage() {
        const text = this.elements.messageInput.value.trim();
        if (!text || !this.isConnected) return;

        this.ws.send(JSON.stringify({
            type: "text",
            username: this.username,
            text,
            timestamp: new Date().toISOString(),
            room: this.room
        }));

        this.elements.messageInput.value = "";
        this.elements.messageInput.style.height = "auto";
    }

    async handleFileSelect(event) {
        const file = event.target.files[0];
        if (!file || !this.isConnected) return;

        if (file.size > 5 * 1024 * 1024) {
            this.updateStatus("File size must be less than 5MB", "disconnected");
            event.target.value = "";
            return;
        }

        try {
            const base64Data = await this.fileToBase64(file);

            this.ws.send(JSON.stringify({
                type: "file",
                username: this.username,
                fileName: file.name,
                fileType: file.type,
                fileSize: file.size,
                fileData: base64Data,
                timestamp: new Date().toISOString(),
                room: this.room
            }));

            event.target.value = "";
        } catch (error) {
            console.error("File upload error:", error);
            this.updateStatus("Failed to upload file", "disconnected");
        }
    }

    fileToBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result.split(",")[1]);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    displayMessage(message) {
        if (message.type === "error") {
            this.updateStatus(message.message || "Something went wrong", "disconnected");
            return;
        }

        const messageElement = document.createElement("div");
        messageElement.className = "message";

        if (message.type === "system") {
            messageElement.classList.add("system");
            messageElement.innerHTML = `
                <div class="message-content">
                    <i class="fas fa-info-circle"></i>
                    ${this.escapeHtml(message.message)}
                </div>
            `;
        } else {
            if (message.username === this.username) {
                messageElement.classList.add("own");
            }

            const timestamp = new Date(message.timestamp).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit"
            });

            if (message.type === "text") {
                messageElement.innerHTML = `
                    <div class="message-header">
                        <span class="username">${this.escapeHtml(message.username)}</span>
                        <span class="timestamp">${timestamp}</span>
                    </div>
                    <div class="message-content">${this.escapeHtml(message.text)}</div>
                `;
            } else if (message.type === "file") {
                const downloadUrl = message.fileUrl || "";
                const safeDownloadUrl = this.escapeHtml(downloadUrl);
                const safeFileName = this.escapeHtml(message.fileName);
                const preview = downloadUrl && message.fileType.startsWith("image/")
                    ? `<div class="file-preview"><img src="${safeDownloadUrl}" alt="${safeFileName}"></div>`
                    : "";
                const fileIcon = this.getFileIcon(message.fileType);

                messageElement.innerHTML = `
                    <div class="message-header">
                        <span class="username">${this.escapeHtml(message.username)}</span>
                        <span class="timestamp">${timestamp}</span>
                    </div>
                    <div class="message-content">
                        <div class="file-summary">
                            <i class="${fileIcon}"></i>
                            <strong>${safeFileName}</strong>
                        </div>
                        <div class="file-size">${this.formatFileSize(message.fileSize)}</div>
                        ${preview}
                        <a href="${safeDownloadUrl}" download="${safeFileName}" class="file-download">
                            <i class="fas fa-download"></i>
                            Download
                        </a>
                    </div>
                `;
            }
        }

        this.elements.messages.appendChild(messageElement);
        this.elements.messages.scrollTop = this.elements.messages.scrollHeight;
    }

    getFileIcon(fileType) {
        if (fileType.startsWith("image/")) return "fas fa-image";
        if (fileType.startsWith("video/")) return "fas fa-video";
        if (fileType.startsWith("audio/")) return "fas fa-music";
        if (fileType.includes("pdf")) return "fas fa-file-pdf";
        if (fileType.includes("document") || fileType.includes("text")) return "fas fa-file-alt";
        return "fas fa-file";
    }

    showWelcomeMessage() {
        if (this.elements.messages.children.length > 0) return;

        const welcomeMessage = document.createElement("div");
        welcomeMessage.className = "message system";
        welcomeMessage.innerHTML = `
            <div class="message-content">
                <i class="fas fa-rocket"></i>
                Connect with your name to start chatting.
            </div>
        `;
        this.elements.messages.appendChild(welcomeMessage);
    }

    toggleUsersPanel() {
        const isOpen = document.body.classList.toggle("users-open");
        this.elements.usersToggle.setAttribute("aria-expanded", String(isOpen));
    }

    closeUsersPanel() {
        document.body.classList.remove("users-open");
        this.elements.usersToggle.setAttribute("aria-expanded", "false");
    }

    escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text || "";
        return div.innerHTML;
    }

    formatFileSize(bytes) {
        if (bytes === 0) return "0 Bytes";
        const k = 1024;
        const sizes = ["Bytes", "KB", "MB", "GB"];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
    }
}

document.addEventListener("DOMContentLoaded", () => {
    new TriChat();
});
