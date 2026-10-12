output "runtime" {
  value       = google_vertex_ai_reasoning_engine.engine.id
  description = "Agent Runtime resource name (what scripts/smoke_runtime.py --runtime takes)"
}

output "sql_connection_name" {
  value = google_sql_database_instance.pg.connection_name
}

output "web_url" {
  value       = var.web_enabled ? google_cloud_run_v2_service.web[0].uri : null
  description = "Open this (signed in as an allowed user) to use the web app"
}

output "image" {
  value = local.image
}
