#! /usr/bin/env python3

from argparse import ArgumentParser
import json
import logging
import pprint
import requests
import sys
import shelve
import subprocess
from time import sleep
from datetime import datetime, timedelta, time

appKey = "I8U8uUExhEzXtPGxITMijwu2A5bgBf1X"
scope = "smartWrite"
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s')
logger = logging.getLogger("quick_home_away")
logging.getLogger('urllib3').setLevel(logging.WARNING)


# Hack to be compatible with wildly different versions of the requests module.
def maybeCall( var ):
    if callable( var ):
        return var()
    else:
        return var

class EcobeeApplication( object ):
    def __init__( self ):
        self.config = shelve.open( "ecobee.shelf" )
        # Assume somebody is home this many minutes after we saw somebody.
        self.homeDecayMinutes = 15
        # Map of thermostat ID to the last revision seen.
        self.lastSeen = {}
        self.ping_addrs = []

    def updateAuthentication( self, response ):
        if not response.ok:
            logger.error("Error updating authentication: %s - %s", maybeCall(response.status_code), maybeCall(response.text))
        assert response.ok
        result = maybeCall( response.json )

        self.config[ "access_token" ] = result[ "access_token" ]
        self.config[ "token_type" ] = result[ "token_type" ]
        self.config[ "refresh_token" ] = result[ "refresh_token" ]
        self.config[ "authentication_expiration" ] = datetime.now() + \
                timedelta( 0, int( result[ "expires_in" ] ) )

    def install( self ):
        r = requests.get(
                "https://api.ecobee.com/authorize?response_type=ecobeePin&client_id=%s&scope=%s"
                % ( appKey, scope ) )
        assert r.ok
        result = maybeCall( r.json )
        authorizationToken = result[ 'code' ]
        print("Please log onto the ecobee web portal, log in, select the menu ")
        print("item in the top right (3 lines), and select MY APPS.")
        print("Next, click Add Application and enter the following ")
        print("authorization code:", result[ 'ecobeePin' ])
        print("Then follow the prompts to add the Quick Home/Away app.")
        print("You have %d minutes." % result[ 'expires_in' ])
        print()
        print("Hit enter when done:", end=' ')
        input()

        r = requests.post(
                "https://api.ecobee.com/token?grant_type=ecobeePin&code=%s&client_id=%s"
                % ( authorizationToken, appKey ) )
        self.updateAuthentication( r )

        print("Installation is complete. Now run this script without any ")
        print("arguments to control your thermostat.")

    def maybeRefreshAuthentication( self ):
        if "authentication_expiration" in self.config and \
                datetime.now() + timedelta( 0, 60 ) < self.config[ "authentication_expiration" ]:
            return
        if 'refresh_token' not in self.config:
            print("We don't have a refresh_token!")
            print("Run '%s --install' to get one." % sys.argv[ 0 ])
            sys.exit( 1 )
        logger.info("Refreshing authentication.")
        r = requests.post(
                "https://api.ecobee.com/token?grant_type=refresh_token&code=%s&client_id=%s"
                % ( self.config[ 'refresh_token' ], appKey ) )
        self.updateAuthentication( r )

    def get( self, call, args ):
        self.maybeRefreshAuthentication()
        r = requests.get(
                "https://api.ecobee.com/1/%s" % call,
                params={ 'json': json.dumps( args ) },
                headers={
                    'Content-Type': 'application/json;charset=UTF-8',
                    'Authorization': "%s %s" % ( self.config[ "token_type" ],
                        self.config[ "access_token" ] ) }
                )
        self.checkResponse(r)
        try:
            return maybeCall( r.json )
        except ValueError:
            logger.exception("Couldn't decode: %s", r.text)
            raise

    def post( self, call, args ):
        self.maybeRefreshAuthentication()
        r = requests.post(
                "https://api.ecobee.com/1/%s" % call,
                data=json.dumps( args ),
                headers={
                    'Content-Type': 'application/json;charset=UTF-8',
                    'Authorization': "%s %s" % ( self.config[ "token_type" ],
                        self.config[ "access_token" ] ) }
                )
        self.checkResponse(r)
        if not r.ok:
            logger.error("post error: %s", r.text)
        assert r.ok
        return maybeCall( r.json )

    def checkResponse( self, r ):
        if r.status_code == requests.codes.unauthorized:
            logger.error("Unauthorized - Clearing refresh token. Response: %s", r.text)
            self.config[ "authentication_expiration" ] = None
            raise ValueError("authentication is expired")

    def thermostatSummary( self ):
        return self.get( "thermostatSummary", {
            "selection": {
                "selectionType": "registered",
                "selectionMatch": "",
                }
            } )

    def thermostat( self, identifiers, includeDevice=False, includeProgram=False,
            includeRuntime=False, includeEvents=False ):
        """Return the contents of thermostatList indexed by identifier."""
        data = self.get( "thermostat", {
            "selection": {
                "selectionType": "thermostats",
                "selectionMatch": ":".join( identifiers ),
                "includeDevice": includeDevice,
                "includeProgram": includeProgram,
                "includeRuntime": includeRuntime,
                "includeEvents": includeEvents
                }
            } )
        return { thermostat[ 'identifier' ]: thermostat
                for thermostat in data[ 'thermostatList' ] }

    def runtimeReport( self, thermostatId, includeSensors=False ):
        start = datetime.now() - timedelta( 1 )
        end = datetime.now() + timedelta( 1 )
        return self.get( "runtimeReport",
                {
                "startDate": start.strftime( "%Y-%m-%d" ),
                "endDate": end.strftime( "%Y-%m-%d" ),
                "includeSensors": includeSensors,
                "selection": {
                    "selectionType": "thermostats",
                    "selectionMatch": thermostatId }
                } )

    def poll( self ):
        """Return a list of thermostat ids that have been updated since the
        last time we polled."""
        summary = self.thermostatSummary()
        updated = []
        if 'revisionList' not in summary:
            logger.warning("Couldn't find revisionList in the following summary object: %s", pprint.pformat(summary) )
            return []

        for revision in summary[ 'revisionList' ]:
            parts = revision.split( ":" )
            identifier = parts[ 0 ]
            name = parts[ 1 ]
            intervalRevision = parts[ 6 ]
            if intervalRevision != self.lastSeen.get( identifier ):
                updated.append( identifier )
                self.lastSeen[ identifier ] = intervalRevision
        return updated

    def setHold( self, thermostatId, thermostatTime, climate, minutes ):
        end = thermostatTime + timedelta( 0, minutes * 60 )
        logger.info("setHold %s from %s until %s", climate, thermostatTime, end )
        self.post( "thermostat",
                {
                    "selection": {
                        "selectionType": "thermostats",
                        "selectionMatch": thermostatId
                        },
                    "functions": [
                        {
                            "type": "setHold",
                            "params": {
                                "holdClimateRef": climate,
                                "startDate": thermostatTime.strftime( "%Y-%m-%d" ),
                                "startTime": thermostatTime.strftime( "%H:%M:%S" ),
                                "endDate": end.strftime( "%Y-%m-%d" ),
                                "endTime": end.strftime( "%H:%M:%S" ),
                                "holdType": "dateTime",
                                #"coolHoldTemp": 780,  # Not used when setting holdClimateRef
                                #"heatHoldTemp": 700,  # Not used when setting holdClimateRef
                                }
                            }
                        ]
                    }
                )

    def thermostatIdentifiers( self ):
        identifiers = []
        for row in self.thermostatSummary()[ 'revisionList' ]:
            identifiers.append( row.split( ':' )[ 0 ] )
        return identifiers

class QuickHomeAway( EcobeeApplication ):
    def ping (self, address):
        ping_command = "ping -c 2 -n -W 4 %s" %(address,)
        child = subprocess.Popen(ping_command,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   shell=True)
        (output, error) = child.communicate()
        logger.debug("ping %s: %s, %s, %s", address, child.returncode, error, output)
        return child.returncode == 0

    def sensorReport( self, thermostatId ):
        result = self.runtimeReport( thermostatId, includeSensors=True )[
                'sensorList' ][ 0 ]
        sensors = {}
        for sensor in result[ 'sensors' ]:
            sensors[ sensor[ 'sensorId' ] ] = sensor
        columns = { name: index for index, name in enumerate( result[ 'columns' ] ) }
        data = []
        for row in result[ 'data' ]:
            parts = row.split( "," )
            dateString = "%s %s" % ( parts[ columns[ "date" ] ],
                    parts[ columns[ "time" ] ] )
            date = datetime.strptime( dateString, "%Y-%m-%d %H:%M:%S" )
            rowData = {}
            for sensor in sensors.values():
                value = parts[ columns[ sensor[ "sensorId" ] ] ]
                if value in ( "", "null" ):
                    continue
                rowData.setdefault( sensor[ "sensorType" ], [] ).append( float( value ) )
            if rowData:
                data.append( ( date, rowData ) )
        return data

    def aggressiveAway( self ):
        updated = self.poll()
        if not updated:
            return
        thermostat = self.thermostat( updated, includeEvents=True,
                includeProgram=True )
        ping_addr_found = None
        if self.ping_addrs:
            for addr in self.ping_addrs:
                if self.ping(addr):
                    ping_addr_found = addr
                    break
        for identifier in updated:
            data = self.sensorReport( identifier )
            sensorClimate = 'away'
            for date, sensorData in data[ -3: ]:
                occupied = sum( sensorData.get('occupancy',[]))
                if not occupied and ping_addr_found:
                    occupied += 1

                if occupied:
                    sensorClimate = 'home'
                logger.info("%s - %s%s", date.strftime( "%H:%M" ),
                        ", ".join( "%s: %s" % ( k, v )
                            for k, v in sensorData.items() ),
                        ", ping address found: %s" % (ping_addr_found) if self.ping_addrs else "")

            logger.info( "Sensors say we're %s.", sensorClimate)

            runningClimateRef = None
            for event in thermostat[ identifier ][ 'events' ]:
                if event[ 'running' ]:
                    runningClimateRef = event[ 'holdClimateRef' ]
                    logger.info("%s %s until %s",  event[ 'type' ], event[ 'holdClimateRef' ],
                            event[ 'endTime' ] )

            if runningClimateRef is None:
                # Maybe we're on the regular schedule
                runningClimateRef = thermostat[ identifier ][ 'program' ][
                        'currentClimateRef' ]
                logger.info("Regularly scheduled climate: %s", runningClimateRef )

            if runningClimateRef in ( 'home', 'away' ):
                if runningClimateRef != sensorClimate:
                    logger.info("Change climate from %s to %s",  runningClimateRef,
                            sensorClimate)
                    thermostatTime = datetime.strptime(
                          thermostat[ identifier ][ 'thermostatTime' ],
                          "%Y-%m-%d %H:%M:%S" )
                    self.setHold( identifier, thermostatTime, sensorClimate, 14 )

    def sensors( self, thermostatId, sensorType ):
        result = self.get( "thermostat", {
            "selection": {
                "selectionType": "thermostats",
                "selectionMatch": thermostatId,
                "includeDevice": True
                }
            } )
        sensors = []
        for thermostat in result[ 'thermostatList' ]:
            for device in thermostat[ 'devices' ]:
                for sensor in device[ 'sensors' ]:
                    if sensor[ 'type' ] == sensorType:
                        sensors.append( sensor )
        return sensors

    def main( self ):
        parser = ArgumentParser()
        parser.add_argument( "--install", action="store_true",
                help="Authorize this application to access your thermostat. "
                "Use this the first time you run the application." )
        parser.add_argument( "--ping", nargs='+', type=str,
                help="In addition to the sensor status, also ping this network address to check for presence.")
        parser.add_argument( "minutes", nargs='?', type=int,
                help="Run this many MINUTES and then exit. If this argument "
                "is omitted, run forever." )
        args = parser.parse_args()

        if args.install:
            self.install()
            return

        if args.ping:
            self.ping_addrs = args.ping
            logger.info("Also pinging the following addresses to check for presence: %s", args.ping)
        if not args.minutes is None:
            endTime = datetime.now() + timedelta( 0, args.minutes * 60 )
            logger.info("Run until %s.",  endTime)

        while True:
            nowTime = datetime.now().time()
            try:
                self.aggressiveAway()
            except Exception:
                logger.exception("Caught error")
            if not args.minutes is None and datetime.now() > endTime:
                break
            sleep( 60 - datetime.now().second )

if __name__ == '__main__':
    app = QuickHomeAway()
    sys.exit( app.main() )

