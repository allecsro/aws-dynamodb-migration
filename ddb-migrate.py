#!/usr/bin/python3

import boto3
import argparse
import progressbar
import time
import threading

from botocore.exceptions import ClientError

# Declare the arguments required to perform the scan
parser = argparse.ArgumentParser()
parser.add_argument("-st", "--source-table", type=str, help="Provide the name of source the table.",
                    nargs='?', required=True)
parser.add_argument("-dt", "--dest-table", type=str, help="Provide the name of destination the table.",
                    nargs='?', required=True)                    
parser.add_argument("-p", "--profile", type=str, help="AWS credentials profile, the profile of the aws credentials as defined in ~/.aws/credentials",
                    nargs='?', default="default" , const=0)
parser.add_argument("-sr", "--source-region", type=str, help="Provide the AWS source region (ex: us-east-1).",
                    nargs='?', default="us-east-1" , const=0)
parser.add_argument("-dr", "--dest-region", type=str, help="Provide the AWS destination region (ex: eu-west-1).",
                    nargs='?', default="eu-west-3" , const=0)                    
parser.add_argument("-s", "--segments", type=int, help="Optional. Represents the total number of segments into which the scan operation will be divided.",
                    nargs='?', default=1 , const=0)
parser.add_argument("-l", "--limit", type=int, help="Optional. The maximum number of items to be scanned per request. This can help prevent situations where one worker consumes all of the provisioned throughput, at the expense of all other workers.",
                    nargs='?', default=500 , const=0)
args = parser.parse_args()

# Initialize DynamoDB tables
session = boto3.Session(profile_name=args.profile)
sourceClient = session.resource("dynamodb", region_name=args.source_region)
destClient = session.resource("dynamodb", region_name=args.dest_region)
sourceTable = sourceClient.Table(args.source_table)
destTable = destClient.Table(args.dest_table)

# Ensure source table exists or wait otherwise to be created
print(f'Checking if source table {sourceTable} exists...')
try:
  sourceTable.table_status in ("ACTIVE")
  print(f'{sourceTable}: OK')
  print(sourceTable.meta)
except ClientError:
    exit(f'Source table {sourceTable} does not exist or is not in active state.')

# Check if the destination table exists or create it otherwise
print(f'Checking if destination table {destTable} exists...')

try:
  destTable.table_status in ("CREATING", "UPDATING", "DELETING", "ACTIVE")
  print(f'{sourceTable}: OK')
except (ClientError) as e:
    params = {
        'TableName': args.dest_table,
        'KeySchema': sourceTable.key_schema,
        'AttributeDefinitions': sourceTable.attribute_definitions,
        'BillingMode': sourceTable.billing_mode_summary['BillingMode'],
    }

    print(f'Destination table {destTable} does not exist. Creating table with params: {params}')

    if (sourceTable.global_secondary_indexes is not None):
        params['GlobalSecondaryIndexes'] = []
        for ndx, globalSecondaryIndex in enumerate(sourceTable.global_secondary_indexes):
            index = {
                'IndexName': globalSecondaryIndex['IndexName'],
                'KeySchema': globalSecondaryIndex['KeySchema'],
                'Projection': globalSecondaryIndex['Projection']
            }

            if (params['BillingMode'] != 'PAY_PER_REQUEST'):
                index['ProvisionedThroughput'] = {
                    'ReadCapacityUnits': globalSecondaryIndex['ProvisionedThroughput']['ReadCapacityUnits'],
                    'WriteCapacityUnits': globalSecondaryIndex['ProvisionedThroughput']['WriteCapacityUnits']
                }

            params['GlobalSecondaryIndexes'].append(index)


    if (sourceTable.local_secondary_indexes is not None):
        params['LocalSecondaryIndexes'] = []
        for ndx, localSecondaryIndex in enumerate(sourceTable.local_secondary_indexes):
            index = {
                'IndexName': localSecondaryIndex['IndexName'],
                'KeySchema': localSecondaryIndex['KeySchema'],
                'Projection': localSecondaryIndex['Projection']
            }

            if (params['BillingMode'] != 'PAY_PER_REQUEST'):
                index['ProvisionedThroughput'] = {
                    'ReadCapacityUnits': localSecondaryIndex['ProvisionedThroughput']['ReadCapacityUnits'],
                    'WriteCapacityUnits': localSecondaryIndex['ProvisionedThroughput']['WriteCapacityUnits']
                }

            params['LocalSecondaryIndexes'].append(index)

    if (params['BillingMode'] != 'PAY_PER_REQUEST'):
        params['ProvisionedThroughput'] = {
            'ReadCapacityUnits': sourceTable.provisioned_throughput['ReadCapacityUnits'],
            'WriteCapacityUnits': sourceTable.provisioned_throughput['WriteCapacityUnits']
        }

    # Create the destination table
    destTable = destClient.create_table(**params)

    # Wait for the destination table to be created
    destTable.meta.client.get_waiter('table_exists').wait(TableName=args.dest_table)



print(f'Migration records from table {sourceTable} on region {args.source_region} into {destTable} on region {args.dest_region}')

aproxItemCount = sourceTable.item_count

print(f'Aprox item count: {aproxItemCount}')


# Define global variables
threadLock = threading.Lock()
totalCount = 0
threads = []

# Initialize the progress bar if the output is verbose
bar = progressbar.ProgressBar(maxval=aproxItemCount, widgets=[progressbar.Bar('=', '[', ']'), ' ', progressbar.Percentage()])
bar.start()

def copy_items(items):
    global totalCount
    global bar
    global aproxItemCount

    count = 0
    with destTable.batch_writer() as batch:
        for item in items:
            batch.put_item(
                Item = item
            )
            count += 1

    with threadLock:
        totalCount += count

        # Update the progress bar, but keep in mind that we might get more items than the initial estimated items count
        bar.update(min(totalCount, aproxItemCount))


class scanThread (threading.Thread):
    def __init__(self, segment, name):
      threading.Thread.__init__(self)
      self.segment = segment
      self.name = name
    def run(self):
        response = sourceTable.scan(
            Select="ALL_ATTRIBUTES",
            TotalSegments=args.segments,
            Segment=self.segment,
            Limit=args.limit,
            ConsistentRead=True
        )

        copy_items(response.get('Items'))

        while 'LastEvaluatedKey' in response: 
            lastEvaluatedKey = response.get('LastEvaluatedKey', None)
            
            response = sourceTable.scan(
                Select="ALL_ATTRIBUTES",
                ConsistentRead=True,
                TotalSegments=args.segments,
                Segment=self.segment,
                Limit=args.limit,
                ExclusiveStartKey=lastEvaluatedKey
            )

            if (response['ResponseMetadata']['HTTPStatusCode'] != 200):
                print(response)
                break
            
            copy_items(response.get('Items'))  



# Record the start time of the script for statistics
start = time.time()

# Start the threads
for i in range(args.segments):
    thread = scanThread(i, f'ScanSegment-{i}')
    thread.start()
    threads.append(thread)


# Wait for all threads to complete
for t in threads:
   t.join()

bar.finish()
print(f'Migrated item count: {totalCount}')
print(f'Execution time {int(time.time() - start)} seconds')