app_name = "accountant_agent"
app_title = "Accountant Agent"
app_publisher = "Marwan Badr"
app_description = "acc agent"
app_email = "marwanbadr@gmail.com"
app_license = "mit"

# Apps
# ------------------

required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "accountant_agent",
# 		"logo": "/assets/accountant_agent/logo.png",
# 		"title": "Accountant Agent",
# 		"route": "/accountant_agent",
# 		"has_permission": "accountant_agent.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/accountant_agent/css/accountant_agent.css"
# app_include_js = "/assets/accountant_agent/js/accountant_agent.js"

# include js, css files in header of web template
# web_include_css = "/assets/accountant_agent/css/accountant_agent.css"
# web_include_js = "/assets/accountant_agent/js/accountant_agent.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "accountant_agent/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "accountant_agent/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "accountant_agent.utils.jinja_methods",
# 	"filters": "accountant_agent.utils.jinja_filters"
# }

# Installation
# ------------

# Provisioning for the Accountant Agent's ERP identity.
#
# Both hooks point at the same idempotent routine on purpose: after_install
# provisions a fresh site, after_migrate repairs a site that was installed
# before this module existed or had the agent user removed. Every step is
# get-or-create, so re-running changes nothing on a healthy site.
#
# What it creates: a permission-less "Accountant Agent" role, an agent User
# holding only that role, and a disabled Agent Write Policy. The agent is
# provisioned but completely inert until a System Manager enables it.
after_install = "accountant_agent.install.after_install"
after_migrate = "accountant_agent.install.after_migrate"

# before_install = "accountant_agent.install.before_install"

# Uninstallation
# ------------

# before_uninstall = "accountant_agent.uninstall.before_uninstall"
# after_uninstall = "accountant_agent.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "accountant_agent.utils.before_app_install"
# after_app_install = "accountant_agent.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "accountant_agent.utils.before_app_uninstall"
# after_app_uninstall = "accountant_agent.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "accountant_agent.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

scheduler_events = {
	"hourly": [
		"accountant_agent.agent_api.services.agent_api_service.cleanup_old_files"
	],
	"daily": [
		# Alerts on Agent Write Log rows stuck IN_FLIGHT. These should be
		# impossible - the reservation and its commit share one transaction - so
		# a survivor is an invariant violation worth an error log, never a quiet
		# cleanup that would destroy the only evidence of the bug.
		"accountant_agent.agent_api.services.agent_write_service.alert_on_stranded_in_flight"
	],
}

# Testing
# -------

# before_tests = "accountant_agent.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "accountant_agent.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "accountant_agent.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["accountant_agent.utils.before_request"]
# after_request = ["accountant_agent.utils.after_request"]

# Job Events
# ----------
# before_job = ["accountant_agent.utils.before_job"]
# after_job = ["accountant_agent.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"accountant_agent.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

