#!/usr/bin/env python3
from azure.identity import DefaultAzureCredential
from azure.mgmt.automation import AutomationClient
from azure.mgmt.automation.models import NonAzureQueryProperties
from azure.mgmt.hybridcompute import HybridComputeManagementClient
from azure.mgmt.loganalytics import LogAnalyticsManagementClient
from azure.loganalytics import LogAnalyticsDataClient
from azure.loganalytics.models import QueryBody
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from datetime import datetime, timezone, timedelta
import automationassets
from automationassets import AutomationAssetNotFound

# Handle query output
import pandas as pd
import sys

# MS API Docs - Automation account
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.automationclient
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.operations.softwareupdateconfigurationsoperations
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.models.softwareupdateconfiguration
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.models.scheduleproperties
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.models.updateconfiguration

# MS API Docs - Log analytics
# https://docs.microsoft.com/en-us/python/api/overview/azure/monitor-query-readme?view=azure-python
# https://jihanjeeth.medium.com/log-retrieval-from-azure-log-analytics-using-python-52e8e8e5e870

# Get variables
automation_account_name = automationassets.get_automation_variable("aa_name")
resource_group_name = automationassets.get_automation_variable("resource_group_name")
subscription_id = automationassets.get_automation_variable("subscription_id")
saved_searche_id = automationassets.get_automation_variable("saved_search_name")

# Instanciate all clients
token_credential = DefaultAzureCredential()
automation_client = AutomationClient(token_credential, subscription_id)
log_analytics_client = LogAnalyticsManagementClient(token_credential, subscription_id)
azure_arc_client = HybridComputeManagementClient(token_credential, subscription_id)
log_analytics_data_client = LogsQueryClient(token_credential)

##################################################################################################################
# 1. GET MACHINES CONNECTED TO THE WORKSPACE
connected_machines = []
query = "Heartbeat | distinct Computer, ResourceGroup, OSType, Resource"
workspace_resource_id = automation_client.linked_workspace.get(resource_group_name, automation_account_name).id
workspace_name = automation_client.linked_workspace.get(resource_group_name, automation_account_name).id.split('/')[-1]
workspace_id = log_analytics_client.workspaces.get(resource_group_name, workspace_name).customer_id
workspace = automation_client.linked_workspace.get(resource_group_name, automation_account_name)
# print(log_analytics_client.saved_searches.get(resource_group_name, workspace_name, "MockComputerGroup"))


# Query Log Analytics to retrieve machine list
end_time=datetime.now(timezone.utc)
start_time = end_time - timedelta(days=1)
response = log_analytics_data_client.query_workspace(workspace_id, query, timespan=(start_time, end_time))

# Handle Log Analytics response
if response.status == LogsQueryStatus.PARTIAL:
	# handle error here
	error = response.partial_error
	data = response.partial_data
	print("Error")
elif response.status == LogsQueryStatus.SUCCESS:
	data = response.tables

for table in data:
	df = pd.DataFrame(data=table.rows, columns=table.columns)
	# print(df)
	for index, row in df.iterrows():
		dic = {'Computer':row[0], 'ResourceGroup':row[1], 'OSType':row[2], 'Resource':row[3]}
		connected_machines.append(dic)

    
##################################################################################################################
# 2. ITERATE OVER ALL SCHEDULERS AND ADD MACHINES TO SCHEDULERS IF NEEDED
update_configurations = automation_client.software_update_configurations.list(resource_group_name,automation_account_name).value
schedule_configurations = automation_client.schedule.list_by_automation_account(resource_group_name,automation_account_name)

# Iterate over update_configuration objects
for update_configuration in update_configurations:
	# Local vars
	schedule = None
	machines_list_updated = False

	# Get current configuration, and remove all machines from list, and get OS type for the current configuration
	software_update_configuration_p = automation_client.software_update_configurations.get_by_name(resource_group_name, automation_account_name, update_configuration.name)
	software_update_configuration_p.update_configuration.non_azure_computer_names.clear()
	update_configuration_os = update_configuration.update_configuration.additional_properties.get('operatingSystem')

	# Retrieve associated schedule
	for schedule_configuration in schedule_configurations:
		# If update configuration and schedules match, then we found the right schedule
		if update_configuration.name in schedule_configuration.name:
			schedule = automation_client.schedule.get(resource_group_name, automation_account_name, schedule_configuration.name)


	# Add machines that need to be added
	for connected_machine in connected_machines:
		# Get connected machine attributes
		machine_rg = connected_machine['ResourceGroup']
		machine_name = connected_machine['Computer']
		machine_resource = connected_machine['Resource']
		machine_os = connected_machine['OSType']
		
		# Get VM using Azure ARC
		try:
			arc_vm = azure_arc_client.machines.get(machine_rg, machine_resource)
		except:
			print("DEBUG: Machine " + machine_name + " not found")
			continue

		# If machine has appropriate tag and OS, add it to the maintenance schedule
		if arc_vm.tags.get('patch'):
			if (machine_os in update_configuration_os) and (arc_vm.tags.get('patch').replace(':','-') in update_configuration.name):
				if machine_name not in software_update_configuration_p.update_configuration.non_azure_computer_names:
					# Add machine to the list
					software_update_configuration_p.update_configuration.non_azure_computer_names.append(machine_name)
					machines_list_updated = True

	
	# Set time properties
	hours = software_update_configuration_p.schedule_info.start_time
	date = datetime(datetime.now().year, datetime.now().month, datetime.now().day)
	software_update_configuration_p.schedule_info.start_time = datetime(date.year, date.month, date.day+1, hours.hour, hours.minute, 0)
	
  # Set saved queries
	non_azure_query = NonAzureQueryProperties()
	non_azure_query.function_alias = saved_searche_id
	non_azure_query.workspace_id = workspace_resource_id
	software_update_configuration_p.update_configuration.targets.non_azure_queries = [non_azure_query]
	
  # Redeploy the update configuration
	automation_client.software_update_configurations.create(resource_group_name, automation_account_name, update_configuration.name, software_update_configuration_p)
	
