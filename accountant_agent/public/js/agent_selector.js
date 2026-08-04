/**
 * Agent Selector Module
 * ---------------------
 * Manages the selection of agent type ('ask', 'analyse', 'audit').
 * Renders an intuitive, elegant agent type switcher in the chat UI.
 * Provides validation rules per agent type.
 */

class AgentSelector {
	constructor(options = {}) {
		this.selected_agent = options.default_agent || 'ask'; // 'ask', 'analyse', or 'audit'
		this.on_change = options.on_change || null;
		this.$container = null;

		this.AGENT_DEFINITIONS = {
			ask: {
				id: 'ask',
				name: __('Auto'),
				icon: 'fa-magic',
				badge_class: 'agent-type-ask',
				description: __('Quick Q&A. Max 5 files, up to 1 MB per file.'),
				rules: {
					max_files: 5,
					max_per_file_mb: 1,
					max_non_excel_total_mb: 1,
					max_excel_total_mb: 1,
					is_aggregate: false
				}
			},
			analyse: {
				id: 'analyse',
				name: __('Analyse Agent'),
				icon: 'fa-bar-chart',
				badge_class: 'agent-type-analyse',
				description: __('In-depth analysis. Max 5 files (15 MB total, Excel up to 20 MB).'),
				rules: {
					max_files: 5,
					max_per_file_mb: 20,
					max_non_excel_total_mb: 15,
					max_excel_total_mb: 20,
					is_aggregate: true
				}
			},
			audit: {
				id: 'audit',
				name: __('Audit Agent'),
				icon: 'fa-search-plus',
				badge_class: 'agent-type-audit',
				description: __('Compliance & Audit. Max 5 files (15 MB total, Excel up to 20 MB).'),
				rules: {
					max_files: 5,
					max_per_file_mb: 20,
					max_non_excel_total_mb: 15,
					max_excel_total_mb: 20,
					is_aggregate: true
				}
			}
		};
	}

	render($parent) {
		let selected = this.AGENT_DEFINITIONS[this.selected_agent];
		this.$container = $(`
			<div class="agent-dropdown-container">
				<button type="button" class="agent-dropdown-toggle-btn ${selected.badge_class}" title="${selected.description}">
					<i class="fa ${selected.icon}"></i>
					<span>${selected.name}</span>
					<i class="fa fa-chevron-up caret-icon"></i>
				</button>
				<div class="agent-dropdown-menu" style="display: none;">
					${Object.keys(this.AGENT_DEFINITIONS).map(key => {
						let agent = this.AGENT_DEFINITIONS[key];
						let is_active = key === this.selected_agent;
						return `
							<div class="agent-dropdown-item ${is_active ? 'active' : ''} ${agent.badge_class}" data-agent="${agent.id}">
								<div class="agent-dropdown-item-header">
									<i class="fa ${agent.icon}"></i>
									<span class="agent-dropdown-item-name">${agent.name}</span>
									${is_active ? '<i class="fa fa-check check-icon"></i>' : ''}
								</div>
								<div class="agent-dropdown-item-desc">${agent.description}</div>
							</div>
						`;
					}).join('')}
				</div>
			</div>
		`);

		// Bind event to toggle dropdown
		this.$container.find('.agent-dropdown-toggle-btn').on('click', (e) => {
			e.stopPropagation();
			this.$container.find('.agent-dropdown-menu').toggle();
			this.$container.find('.agent-dropdown-toggle-btn').toggleClass('open');
		});

		// Bind event to item selection
		this.$container.find('.agent-dropdown-item').on('click', (e) => {
			let agent_id = $(e.currentTarget).data('agent');
			this.set_agent(agent_id);
			this.$container.find('.agent-dropdown-menu').hide();
			this.$container.find('.agent-dropdown-toggle-btn').removeClass('open');
		});

		// Close dropdown when clicking outside
		$(document).on('click', (e) => {
			if (!$(e.target).closest('.agent-dropdown-container').length) {
				this.$container.find('.agent-dropdown-menu').hide();
				this.$container.find('.agent-dropdown-toggle-btn').removeClass('open');
			}
		});

		if ($parent) {
			$parent.append(this.$container);
		}
		return this.$container;
	}

	set_agent(agent_id) {
		if (!this.AGENT_DEFINITIONS[agent_id]) return;
		if (this.selected_agent === agent_id) return;

		this.selected_agent = agent_id;
		let agent = this.AGENT_DEFINITIONS[agent_id];

		if (this.$container) {
			// Update the toggle button
			let $toggle = this.$container.find('.agent-dropdown-toggle-btn');
			$toggle.attr('class', `agent-dropdown-toggle-btn ${agent.badge_class}`);
			$toggle.attr('title', agent.description);
			$toggle.html(`
				<i class="fa ${agent.icon}"></i>
				<span>${agent.name}</span>
				<i class="fa fa-chevron-up caret-icon"></i>
			`);

			// Update active class in list items
			this.$container.find('.agent-dropdown-item').removeClass('active');
			let $selected_item = this.$container.find(`.agent-dropdown-item[data-agent="${agent_id}"]`);
			$selected_item.addClass('active');

			// Update check icons
			this.$container.find('.check-icon').remove();
			$selected_item.find('.agent-dropdown-item-header').append('<i class="fa fa-check check-icon"></i>');
		}

		if (typeof this.on_change === 'function') {
			this.on_change(agent_id, this.get_rules());
		}
	}

	get_selected_agent() {
		return this.selected_agent;
	}

	get_rules() {
		return this.AGENT_DEFINITIONS[this.selected_agent].rules;
	}
}
