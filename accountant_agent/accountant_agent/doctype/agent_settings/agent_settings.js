// Copyright (c) 2026, Marwan Badr and contributors
// For license information, please see license.txt

frappe.ui.form.on("Agent Settings", {
	refresh(frm) {
		trigger_usage_load(frm);
		if (frm.fields_dict.custom_instructions && frm.fields_dict.custom_instructions.input) {
			$(frm.fields_dict.custom_instructions.input).attr('maxlength', 20000);
		}
	},
	onload(frm) {
		trigger_usage_load(frm);
	},
	validate(frm) {
		if (frm.doc.custom_instructions && frm.doc.custom_instructions.length > 20000) {
			frappe.msgprint({
				title: __('Validation Error'),
				indicator: 'red',
				message: __('Custom Instructions cannot exceed 2000 characters.')
			});
			frappe.validated = false;
		}
	}
});

function trigger_usage_load(frm) {
	let email = frm.doc.email || (frm.doc.name && frm.doc.name.includes("@") ? frm.doc.name : null);
	if (email && !frm.is_new()) {
		load_usage_stats(frm, email);
	}
}

function get_usage_container(frm) {
	if (frm.fields_dict.usage_html && frm.fields_dict.usage_html.wrapper) {
		return $(frm.fields_dict.usage_html.wrapper);
	}
	
	let $existing = $(frm.wrapper).find('.agent-usage-dynamic-container');
	if ($existing.length) return $existing;

	let $container = $('<div class="agent-usage-dynamic-container" style="margin-top: 20px; width: 100%;"></div>');
	
	if (frm.fields_dict.custom_instructions && frm.fields_dict.custom_instructions.wrapper) {
		$container.insertAfter($(frm.fields_dict.custom_instructions.wrapper));
	} else if (frm.fields_dict.access_token && frm.fields_dict.access_token.wrapper) {
		$container.insertAfter($(frm.fields_dict.access_token.wrapper));
	} else {
		let $target = $(frm.wrapper).find('.form-page, .form-section, .frappe-control').last();
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
			<i class="fa fa-spinner fa-spin"></i> ${__('Loading usage statistics...')}
		</div>
	`);

	frappe.call({
		method: "accountant_agent.accountant_agent.doctype.agent_settings.agent_settings.get_user_usage",
		args: { email: email },
		callback: function(r) {
			if (r.message) {
				render_usage_dashboard(frm, r.message, email);
			} else {
				$wrapper.html(`
					<div class="alert alert-warning" style="margin: 10px 0; border-radius: 8px;">
						${__('Unable to retrieve usage data for this account.')}
					</div>
				`);
			}
		}
	});
}

function render_usage_dashboard(frm, data, email) {
	let daily = data.daily_usage_percentage || 0.0;
	let total = data.total_usage_percentage || 0.0;

	function get_status_theme(val) {
		if (val >= 90) return { bg: '#fee2e2', text: '#dc2626', bar: 'linear-gradient(90deg, #ef4444, #f87171)' };
		if (val >= 70) return { bg: '#fef3c7', text: '#d97706', bar: 'linear-gradient(90deg, #f59e0b, #fbbf24)' };
		return { bg: '#d1fae5', text: '#059669', bar: 'linear-gradient(90deg, #10a37f, #34d399)' };
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
						${__('API Resource Usage')}
					</h4>
				</div>
				<button class="btn btn-default btn-xs btn-refresh-usage" style="border-radius: 6px; font-weight: 500;">
					<i class="fa fa-refresh"></i> ${__('Refresh Stats')}
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
							${__('Daily Limit Usage')}
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
						${__('Resets every 24 hours')}
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
							${__('Billing Cycle Usage')}
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
						${__('30-day billing cycle usage')}
					</p>
				</div>
			</div>
		</div>
	`;

	let $wrapper = get_usage_container(frm);
	$wrapper.html(html);

	$wrapper.find('.btn-refresh-usage').on('click', function(e) {
		e.preventDefault();
		load_usage_stats(frm, email);
	});
}
