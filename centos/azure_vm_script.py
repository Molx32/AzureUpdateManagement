#!/usr/bin/env python3
######################################################################################################################################################
# MS API Docs - Automation account
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.automationclient
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.operations.softwareupdateconfigurationsoperations
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.models.softwareupdateconfiguration
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.models.scheduleproperties
# https://docs.microsoft.com/en-us/python/api/azure-mgmt-automation/azure.mgmt.automation.models.updateconfiguration

# MS API Docs - Log analytics
# https://docs.microsoft.com/en-us/python/api/overview/azure/monitor-query-readme?view=azure-python
# https://jihanjeeth.medium.com/log-retrieval-from-azure-log-analytics-using-python-52e8e8e5e870
######################################################################################################################################################

# Azure packages
from azure.identity import DefaultAzureCredential
from azure.mgmt.automation import AutomationClient
from azure.mgmt.automation.models import NonAzureQueryProperties, ScheduleCreateOrUpdateParameters, ScheduleFrequency, SoftwareUpdateConfiguration, UpdateConfiguration, ScheduleProperties, LinuxProperties, LinuxUpdateClasses, OperatingSystemType
from azure.mgmt.loganalytics import LogAnalyticsManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.loganalytics import LogAnalyticsDataClient
from azure.loganalytics.models import QueryBody
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from datetime import datetime, timezone, timedelta
import automationassets
from automationassets import AutomationAssetNotFound
from azure.mgmt.subscription import SubscriptionClient

# Various pacakges
import pandas as pd

# System packages
import sys
import json
import re
import pytz

##################################################################################################################
# 0. GET AUTOMATION ACCOUNT VARIABLES
automation_account_name = automationassets.get_automation_variable("aa_name")
resource_group_name = automationassets.get_automation_variable("resource_group_name")
subscription_id = automationassets.get_automation_variable("subscription_id")

# Instanciate all clients
token_credential = DefaultAzureCredential()
automation_client = AutomationClient(token_credential, subscription_id)
log_analytics_client = LogAnalyticsManagementClient(token_credential, subscription_id)
log_analytics_data_client = LogsQueryClient(token_credential)
subscriptions_client = SubscriptionClient(token_credential)


DAYS = {
	"MON":0,
	"TUE":1,
	"WED":2,
	"THU":3,
	"FRI":4,
	"SAT":5,
	"SUN":6
}
#regex = re.compile("^CENTOS-[RPQ]-(MON|TUE|WED|THU|FRI|SAT|SUN)-(03|12|22):00$")
# Match only non prod machines
regex = re.compile("^CENTOS-[RQ]-(MON|TUE|WED|THU|FRI|SAT|SUN)-(03|12|22):00$")

###############################################################
# 1. GET AND SANITIZE ALL CENTOS VMs THAT NEED TO BE PATCHED
# 	 First, we check that the 'patch' tag is compliant
# 	 Then, We check that the OS is indeed CentOS

centOSs = []
for subscription in subscriptions_client.subscriptions.list():
	compute_management_client = ComputeManagementClient(token_credential, subscription.subscription_id)
	#print("Successfully instanciated using " + str(subscription) + "subscription")
	for item in compute_management_client.virtual_machines.list_all():
		# Check PATCH value
		if not item.tags or not 'patch' in item.tags.keys():
			continue
		if not regex.search(item.tags['patch']):
			continue

		# Check OS value
		if item.storage_profile.image_reference:
			# Check when OS is custom
			if item.storage_profile.image_reference.id and "CentOS" in item.storage_profile.image_reference.id:
				centOSs.append(item)
			# Check when OS is Azure based
			elif item.storage_profile.image_reference.offer and "CentOS" in item.storage_profile.image_reference.offer:
				centOSs.append(item)
			else:
				continue
		else:
			continue

##############################################################################
# 2. RETIEVE ALL SECURITY AND CRITICAL UPDATES NEEDED BY ALL LINUX MACHINES
# 	 All machines that are not in this table :
# 		a. Do not report to the Log Analytics => To troubleshoot
# 		b. Or do not need update => Fine
query = '''Update
		| where TimeGenerated>ago(5h) and OSType=="Linux"
		| summarize hint.strategy=partitioned arg_max(TimeGenerated, UpdateState, Classification, BulletinUrl, BulletinID) by ResourceId, Computer, SourceComputerId, Product, ProductArch
		| where UpdateState=~"Needed"
		| project-away UpdateState, TimeGenerated
		| summarize computersCount=dcount(SourceComputerId, 2), ClassificationWeight=max(iff(Classification has "Critical", 4, iff(Classification has "Security", 2, 1))) by ResourceId, Computer, id=strcat(Product, "_", ProductArch), displayName=Product, productArch=ProductArch, classification=Classification, InformationId=BulletinID, InformationUrl=tostring(split(BulletinUrl, ";", 0)[0]), osType=1
		| sort by ClassificationWeight desc, computersCount desc, displayName asc
		| extend informationLink=(iff(isnotempty(InformationId) and isnotempty(InformationUrl), toobject(strcat('{ "uri": "', InformationUrl, '", "text": "', InformationId, '", "target": "blank" }')), toobject('')))
		| project-away ClassificationWeight, InformationId, InformationUrl
		| where classification has "Security" or classification has "Critical"'''
workspace_name 			= automation_client.linked_workspace.get(resource_group_name, automation_account_name).id.split('/')[-1]
workspace_id 			= log_analytics_client.workspaces.get(resource_group_name, workspace_name).customer_id
workspace 				= automation_client.linked_workspace.get(resource_group_name, automation_account_name)

# SEND REQUEST
end_time	= datetime.now(pytz.timezone("Europe/Paris"))
start_time 	= end_time - timedelta(days=1)
response 	= log_analytics_data_client.query_workspace(workspace_id, query, timespan=(start_time, end_time))

if response.status == LogsQueryStatus.PARTIAL:
	error = response.partial_error
	data = response.partial_data
	print("ERROR, unknown error when requesting Log Analytics")
elif response.status == LogsQueryStatus.SUCCESS:
	data = response.tables


##############################################################################
# 3. ITERATE OVER OUR CENTOS :
#	Step 1 - Get needed updates
#	Step 2 - Calculate next schedule
#	Step 3 - Create the deployment schedule
df = pd.DataFrame(data=data[0].rows, columns=data[0].columns)
for centOS in centOSs:
	# Step 1 - Get needed updates
	updates = []
	# The following line filters dataFrame to return all lines for which
	# the column ResourceId contains centOS.id.
	# Note : we convert all strings to lower() to avoid any issue
	updates_df = df[df['ResourceId'].str.lower().str.contains(centOS.id.lower())]
	for index, row in updates_df.iterrows():
		updates.append(row[3])
	if len(updates) == 0:
		print("This " + centOS.id + " do not need to be patched")
		continue
	
	# Step 2 - Calculate next schedule
	# Get info from tag, and get current date
	target_day = DAYS[centOS.tags['patch'].split('-')[2]]
	target_hour= int(centOS.tags['patch'].split('-')[3].split(':')[0])
	now = datetime.now()

	# Find how many days until the next update
	# E.g. if the script runs SUNDAY, and we need to patch WEDNESDAY :
	#	SUNDAY - WEDNESDAY = 6 - 3 = 3 days
	# So next occurence will be the current date + 3 days.
	days_from_now = (target_day - now.weekday()) % 7
	target_date = now + timedelta(days=days_from_now)

	# Modify our target date with appropriate hours, minutes and seconds
	target_date = datetime(target_date.year, target_date.month, target_date.day, target_hour, 0, 0)
	# This conditions handles the case where days_from_now = 0
	if target_date < now:
		target_date = target_date + timedelta(days=7)

	# Step 3 - Create the deployment schedule
	schedule_info = ScheduleProperties(
		start_time 	= target_date,
		time_zone 	= "Europe/Paris",
		is_enabled 	= True,
		frequency 	= ScheduleFrequency.ONE_TIME)

	linux_properties = LinuxProperties(
		included_package_classifications	= None,
		included_package_name_masks 		= updates,
		reboot_setting 						= "Always")

	update_configuration = UpdateConfiguration(
		operating_system 		= OperatingSystemType.LINUX,
		linux 					= linux_properties,
		duration 				= timedelta(hours=2),
		azure_virtual_machines 	= [centOS.id])
	
	software_update_configuration = SoftwareUpdateConfiguration(
		update_configuration 	= update_configuration,
		schedule_info 			= schedule_info)
	
	
	#software_update_configuration_name = "ari-iaasupdate-we-securityux-CENTOS-" + centOS.name
	software_update_configuration_name = "ari-iaasupdate-we-securityux-" + centOS.tags['patch'] + " => " + centOS.name
	automation_client.software_update_configurations.create(resource_group_name, automation_account_name, software_update_configuration_name, software_update_configuration)
	print("VERBOSE, new deployment schedule created for CentOS VM : " + centOS.id)
