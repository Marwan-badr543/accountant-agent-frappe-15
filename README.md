# Accountant Agent for Frappe 15

An AI-powered accounting assistant app integrated directly into your Frappe & ERPNext version 15 instance. This application exposes a custom interface inside ERPNext and communicates securely with the external Accountant Agent server.

## Features

- **Multi-Agent Modes:**
  - **Ask Mode:** Quick accounting questions and basic document processing (up to 1 MB per file).
  - **Analyse Mode:** Detailed analysis of financial reports, ledgers, and transactions.
  - **Audit Mode:** Interactive auditing and compliance checks on uploaded data.
- **Secure Authentication:** Single Sign-On proxy registration and login via JWT tokens stored securely on the server.
- **Modern Chat Interface:** ChatGPT/Claude-style frontend with real-time response rendering, typing animation, and question clarification popups.
- **Attachment Support:** Select or drag-and-drop financial files (PDF, Docx, Excel, CSV, WebP, WebM, WebR, PNG, JPG) with custom size limits tailored to the chosen agent type.

---

## Installation & Setup

1. **Get the App:**
   ```bash
   bench get-app accountant_agent # Accountant Agent for Frappe 15
   ```

An AI-powered accounting assistant app integrated directly into your Frappe & ERPNext version 15 instance. This application exposes a custom interface inside ERPNext and communicates securely with the external Accountant Agent server.

## Features

- **Multi-Agent Modes:**
  - **Ask Mode:** Quick accounting questions and basic document processing (up to 1 MB per file).
  - **Analyse Mode:** Detailed analysis of financial reports, ledgers, and transactions.
  - **Audit Mode:** Interactive auditing and compliance checks on uploaded data.
- **Secure Authentication:** Single Sign-On proxy registration and login via JWT tokens stored securely on the server.
- **Modern Chat Interface:** ChatGPT/Claude-style frontend with real-time response rendering, typing animation, and question clarification popups.
- **Attachment Support:** Select or drag-and-drop financial files (PDF, Docx, Excel, CSV, WebP, WebM, WebR, PNG, JPG) with custom size limits tailored to the chosen agent type.

---

## Installation & Setup

1. **Get the App:**
   ```bash
   bench get-app accountant_agent https://github.com/Marwan-badr543/accountant-agent-frappe-15
   ```

2. **Install on Your Site:**
   ```bash
   bench --site [your-site-name] install-app accountant_agent
   ```

3. **Migrate the Database:**
   ```bash
   bench --site [your-site-name] migrate
   ```

4. **Build Assets & Restart:**
   ```bash
   bench build
   bench restart
   ```

   ```

2. **Install on Your Site:**
   ```bash
   bench --site [your-site-name] install-app accountant_agent
   ```

3. **Migrate the Database:**
   ```bash
   bench --site [your-site-name] migrate
   ```

4. **Build Assets & Restart:**
   ```bash
   bench build
   bench restart
   ```
