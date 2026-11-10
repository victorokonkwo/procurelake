output "alarm_names" {
  description = "Alarms managed by this stack."
  value = concat(
    [for a in aws_cloudwatch_metric_alarm.state_machine_failed : a.alarm_name],
    [aws_cloudwatch_metric_alarm.cdc_freshness.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.cdc_quality : a.alarm_name],
  )
}

output "dashboard_name" {
  description = "Operations dashboard."
  value       = aws_cloudwatch_dashboard.operations.dashboard_name
}
