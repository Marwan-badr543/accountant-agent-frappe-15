// Copyright (c) 2026, Marwan Badr and contributors
// For license information, please see license.txt

frappe.ui.form.on("Agent Settings", {
	refresh(frm) {
		trigger_usage_load(frm);
		if (frm.fields_dict.custom_instructions && frm.fields_dict.custom_instructions.input) {
			$(frm.fields_dict.custom_instructions.input).attr("maxlength", 20000);
		}
	},
	onload(frm) {
		trigger_usage_load(frm);
	},
	validate(frm) {
		if (frm.doc.custom_instructions && frm.doc.custom_instructions.length > 20000) {
			frappe.msgprint({
				title: __("Validation Error"),
				indicator: "red",
				message: __("Custom Instructions cannot exceed 2000 characters."),
			});
			frappe.validated = false;
		}
	},
});

function trigger_usage_load(frm) {
	let email =
		frm.doc.email || (frm.doc.name && frm.doc.name.includes("@") ? frm.doc.name : null);
	if (email && !frm.is_new()) {
		load_usage_stats(frm, email);
	}
}

function get_usage_container(frm) {
	if (frm.fields_dict.usage_html && frm.fields_dict.usage_html.wrapper) {
		return $(frm.fields_dict.usage_html.wrapper);
	}

	let $existing = $(frm.wrapper).find(".agent-usage-dynamic-container");
	if ($existing.length) return $existing;

	let $container = $(
		'<div class="agent-usage-dynamic-container" style="margin-top: 20px; width: 100%;"></div>'
	);

	if (frm.fields_dict.custom_instructions && frm.fields_dict.custom_instructions.wrapper) {
		$container.insertAfter($(frm.fields_dict.custom_instructions.wrapper));
	} else if (frm.fields_dict.access_token && frm.fields_dict.access_token.wrapper) {
		$container.insertAfter($(frm.fields_dict.access_token.wrapper));
	} else {
		let $target = $(frm.wrapper).find(".form-page, .form-section, .frappe-control").last();
		if ($target.length) {
			$container.insertAfter($target);
		} else {
			$container.appendTo($(frm.wrapper));
		}
	}
	return $container;
}

function load_usage_stats(frm, email) {
	let $wrapper = get_usage_container(frm);
	if (!$wrapper || !$wrapper.length) return;

	$wrapper.html(`
		<div style="padding: 15px; text-align: center; color: var(--text-muted, #6b7280);">
			<i class="fa fa-spinner fa-spin"></i> ${__("Loading usage statistics...")}
		</div>
	`);

	frappe.call({
		method: "razyyn.razyyn.doctype.agent_settings.agent_settings.get_user_usage",
		args: { email: email },
		callback: function (r) {
			if (r.message) {
				render_usage_dashboard(frm, r.message, email);
			} else {
				$wrapper.html(`
					<div class="alert alert-warning" style="margin: 10px 0; border-radius: 8px;">
						${__("Unable to retrieve usage data for this account.")}
					</div>
				`);
			}
		},
	});
}

function render_usage_dashboard(frm, data, email) {
	let daily = data.daily_usage_percentage || 0.0;
	let total = data.total_usage_percentage || 0.0;

	function get_status_theme(val) {
		if (val >= 90)
			return {
				bg: "#fee2e2",
				text: "#dc2626",
				bar: "linear-gradient(90deg, #ef4444, #f87171)",
			};
		if (val >= 70)
			return {
				bg: "#fef3c7",
				text: "#d97706",
				bar: "linear-gradient(90deg, #f59e0b, #fbbf24)",
			};
		return { bg: "#d1fae5", text: "#059669", bar: "linear-gradient(90deg, #10a37f, #34d399)" };
	}

	let daily_theme = get_status_theme(daily);
	let total_theme = get_status_theme(total);

	let html = `
		<div class="agent-usage-card" style="
			background: var(--card-bg, #ffffff);
			border: 1px solid var(--border-color, #e5e7eb);
			border-radius: 12px;
			padding: 20px;
			margin-top: 15px;
			margin-bottom: 20px;
			box-shadow: 0 4px 15px rgba(0, 0, 0, 0.03);
			font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
		">
			<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
				<div style="display: flex; align-items: center; gap: 8px;">
					<i class="fa fa-pie-chart" style="color: #10a37f; font-size: 18px;"></i>
					<h4 style="margin: 0; font-weight: 700; font-size: 16px; color: var(--text-color, #111827);">
						${__("API Resource Usage")}
					</h4>
				</div>
				<button class="btn btn-default btn-xs btn-refresh-usage" style="border-radius: 6px; font-weight: 500;">
					<i class="fa fa-refresh"></i> ${__("Refresh Stats")}
				</button>
			</div>

			<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px;">
				<!-- Daily Usage Progress Bar -->
				<div style="
					background: var(--bg-color, #f9fafb);
					padding: 16px;
					border-radius: 10px;
					border: 1px solid var(--border-color, #f3f4f6);
				">
					<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
						<span style="font-size: 13px; font-weight: 600; color: var(--text-color, #374151);">
							${__("Daily Limit Usage")}
						</span>
						<span style="
							background-color: ${daily_theme.bg};
							color: ${daily_theme.text};
							font-size: 12px;
							font-weight: 700;
							padding: 2px 10px;
							border-radius: 12px;
						">
							${daily}%
						</span>
					</div>
					<div style="
						height: 10px;
						background-color: #e5e7eb;
						border-radius: 10px;
						overflow: hidden;
					">
						<div style="
							height: 100%;
							width: ${Math.min(daily, 100)}%;
							background: ${daily_theme.bar};
							border-radius: 10px;
							transition: width 0.6s ease-in-out;
						"></div>
					</div>
					<p style="font-size: 11.5px; color: var(--text-muted, #6b7280); margin-top: 8px; margin-bottom: 0;">
						${__("Resets every 24 hours")}
					</p>
				</div>

				<!-- Total Plan Usage Progress Bar -->
				<div style="
					background: var(--bg-color, #f9fafb);
					padding: 16px;
					border-radius: 10px;
					border: 1px solid var(--border-color, #f3f4f6);
				">
					<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
						<span style="font-size: 13px; font-weight: 600; color: var(--text-color, #374151);">
							${__("Billing Cycle Usage")}
						</span>
						<span style="
							background-color: ${total_theme.bg};
							color: ${total_theme.text};
							font-size: 12px;
							font-weight: 700;
							padding: 2px 10px;
							border-radius: 12px;
						">
							${total}%
						</span>
					</div>
					<div style="
						height: 10px;
						background-color: #e5e7eb;
						border-radius: 10px;
						overflow: hidden;
					">
						<div style="
							height: 100%;
							width: ${Math.min(total, 100)}%;
							background: ${total_theme.bar};
							border-radius: 10px;
							transition: width 0.6s ease-in-out;
						"></div>
					</div>
					<p style="font-size: 11.5px; color: var(--text-muted, #6b7280); margin-top: 8px; margin-bottom: 0;">
						${__("30-day billing cycle usage")}
					</p>
				</div>
			</div>
		</div>
	`;

	let $wrapper = get_usage_container(frm);
	$wrapper.html(html);

	$wrapper.find(".btn-refresh-usage").on("click", function (e) {
		e.preventDefault();
		load_usage_stats(frm, email);
	});
}

// ─────────────────────────────────────────────────────────────────────────────
// Creator Agent — recording access
//
// A SECOND form handler rather than an edit to the one above. Frappe runs every
// registered handler for a DocType, so the usage dashboard and this setup card
// stay independent: neither can break the other, and they can be changed by
// different people without conflicting.
//
// Every figure rendered here comes from get_write_connection_status, which reads
// THIS site only and makes no network call. A status screen that asked the
// platform would report "not connected" whenever the platform was briefly
// unreachable, and the customer would press Connect on a connection that was
// already healthy.
// ─────────────────────────────────────────────────────────────────────────────

frappe.ui.form.on("Agent Settings", {
	refresh(frm) {
		if (frm.is_new()) return;
		render_write_setup(frm);
	},
});

const CONNECT_METHOD = "razyyn.connect";

function agent_email_of(frm) {
	return frm.doc.email || (frm.doc.name && frm.doc.name.includes("@") ? frm.doc.name : null);
}

function get_write_setup_container(frm) {
	// Prefer the declared HTML field. The fallback exists because returning
	// early would make the whole card vanish silently — no card, no error, and
	// no way for anyone to tell whether setup is broken or simply absent. A
	// setup screen that can disappear without saying so is worse than an ugly
	// one.
	if (frm.fields_dict.write_setup_html && frm.fields_dict.write_setup_html.wrapper) {
		return $(frm.fields_dict.write_setup_html.wrapper);
	}

	const existing = $(frm.wrapper).find(".agent-write-setup-container");
	if (existing.length) return existing;

	const $container = $(
		'<div class="agent-write-setup-container" style="margin-top:20px;width:100%;"></div>'
	);
	const $page = $(frm.wrapper).find(".form-page").first();
	$container.appendTo($page.length ? $page : $(frm.wrapper));
	return $container;
}

function render_write_setup(frm) {
	const $wrapper = get_write_setup_container(frm);
	if (!$wrapper || !$wrapper.length) return;

	$wrapper.html(`<div style="padding:12px;color:var(--text-muted,#6b7280);">
		<i class="fa fa-spinner fa-spin"></i> ${__("Checking setup...")}</div>`);

	frappe.call({
		method: `${CONNECT_METHOD}.get_write_connection_status`,
		args: { agent_email: agent_email_of(frm) },
		callback(r) {
			if (r.message) draw_write_card(frm, r.message);
		},
		error() {
			// only_for("System Manager") refuses non-admins. That is not an
			// error worth a red box on their own settings page.
			$wrapper.html(`<div class="text-muted" style="padding:12px;">
				${__("Only a System Manager can set up recording access.")}</div>`);
		},
	});
}

function draw_write_card(frm, s) {
	const $wrapper = get_write_setup_container(frm);
	if (!$wrapper || !$wrapper.length) return;

	const ready =
		s.connected_to_platform &&
		s.recording_enabled &&
		s.policy_enabled &&
		(s.allowed_doctypes || []).length;

	const pill = ready
		? { bg: "#e8f5e9", fg: "#2e7d32", text: __("Ready to record") }
		: s.connected_to_platform
		? { bg: "#fef3c7", fg: "#d97706", text: __("Connected — setup incomplete") }
		: { bg: "#fee2e2", fg: "#dc2626", text: __("Not connected") };

	const esc = (v) => frappe.utils.escape_html(String(v == null ? "" : v));

	const steps = (s.missing || [])
		.map((m) => `<li style="margin-bottom:6px;">${esc(m)}</li>`)
		.join("");

	const problem = s.last_error
		? `<div class="alert alert-warning" style="margin-top:12px;border-radius:8px;">
			 <b>${__("Last problem")}:</b> ${esc(s.last_error)}</div>`
		: "";

	$wrapper.html(`
		<div style="border:1px solid var(--border-color,#e5e7eb);border-radius:12px;
					padding:20px;background:var(--card-bg,#fff);">
			<div style="display:flex;justify-content:space-between;align-items:center;">
				<h4 style="margin:0;font-size:16px;font-weight:700;">
					${__("Let the agent record entries in this ERP")}</h4>
				<span style="background:${pill.bg};color:${pill.fg};font-size:12px;
							 font-weight:700;padding:3px 12px;border-radius:12px;">
					${pill.text}</span>
			</div>

			<p style="color:var(--text-muted,#6b7280);font-size:13px;margin:10px 0 0;">
				${__("The agent acts as its own ERP user")}
				(<code>${esc(s.agent_user)}</code>).
				${__(
					"It can only do what you grant that user here, in your own permissions. Nothing you do on this page gives it access to anything else."
				)}
			</p>

			${
				steps
					? `<div style="margin-top:14px;">
				<div style="font-weight:600;font-size:13px;margin-bottom:6px;">
					${__("Remaining steps")}</div>
				<ul style="font-size:13px;color:var(--text-color,#374151);padding-left:18px;margin:0;">
					${steps}</ul></div>`
					: ""
			}

			${problem}

			<div style="margin-top:18px;display:flex;gap:8px;flex-wrap:wrap;">
				<button class="btn btn-primary btn-sm" data-act="connect">
					${s.connected_to_platform ? __("Reconnect") : __("Connect")}</button>
				<button class="btn btn-default btn-sm" data-act="recommend">
					${__("Apply recommended setup")}</button>
				<button class="btn btn-default btn-sm" data-act="recording"
					${s.connected_to_platform ? "" : "disabled"}>
					${s.recording_enabled ? __("Switch recording off") : __("Switch recording on")}</button>
				<button class="btn btn-default btn-sm" data-act="rotate"
					${s.connected_to_platform ? "" : "disabled"}>
					${__("Issue new credentials")}</button>
				<button class="btn btn-danger btn-sm" data-act="disconnect"
					${s.connected_to_platform ? "" : "disabled"}>
					${__("Disconnect")}</button>
			</div>
		</div>
	`);

	$wrapper.find("[data-act]").on("click", function (e) {
		e.preventDefault();
		handle_write_action(frm, $(this).data("act"), s);
	});
}

function handle_write_action(frm, action, s) {
	const email = agent_email_of(frm);
	const done = (r) => {
		if (r && r.message && r.message.message) {
			frappe.show_alert({ message: r.message.message, indicator: "green" }, 7);
		}
		render_write_setup(frm);
	};

	if (action === "connect") {
		frappe.call({
			method: `${CONNECT_METHOD}.connect_write_access`,
			args: { agent_email: email, enable_recording: 0 },
			freeze: true,
			freeze_message: __("Connecting your ERP..."),
			callback: done,
		});
		return;
	}

	if (action === "recommend") {
		frappe.confirm(
			__(
				"This grants the agent read access to your chart of accounts and lets it prepare draft journal entries. If the write policy is off, dry run is switched on so nothing is saved. Continue?"
			),
			() =>
				frappe.call({
					method: `${CONNECT_METHOD}.apply_recommended_setup`,
					freeze: true,
					callback: done,
				})
		);
		return;
	}

	if (action === "recording") {
		const turning_on = !s.recording_enabled;
		const go = () =>
			frappe.call({
				method: `${CONNECT_METHOD}.set_recording_enabled`,
				args: { agent_email: email, enabled: turning_on ? 1 : 0 },
				freeze: true,
				callback: done,
			});
		if (!turning_on) return go();
		frappe.confirm(
			__(
				"The agent will be able to record entries in this ERP, within the limits of your Agent Write Policy and the permissions you granted its user. Continue?"
			),
			go
		);
		return;
	}

	if (action === "rotate") {
		frappe.confirm(
			__(
				"New credentials will be issued for the agent's ERP user. Any other Razyyn account connected to this same site will stop working until it reconnects. Continue?"
			),
			() =>
				frappe.call({
					method: `${CONNECT_METHOD}.rotate_and_reconnect`,
					args: { agent_email: email },
					freeze: true,
					callback: done,
				})
		);
		return;
	}

	if (action === "disconnect") {
		frappe.confirm(
			__(
				"The agent will no longer be able to record anything in this ERP. Its history in Agent Write Log is kept. Continue?"
			),
			() =>
				frappe.call({
					method: `${CONNECT_METHOD}.disconnect_write_access`,
					args: { agent_email: email },
					freeze: true,
					callback: done,
				})
		);
	}
}
