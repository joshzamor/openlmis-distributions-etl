import csv
from datetime import datetime
import psycopg2
import psycopg2.extensions
import psycopg2.extras
import re
import types
from pprint import pprint
psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)
psycopg2.extensions.register_type(psycopg2.extensions.UNICODEARRAY)


"""
Basic ETL script to denormalize, combine and do any necessary modifications to report on facility visits.  

Limitations:
 - Visit codes are not generated by open-lmis.  They are generated here but assume the one schedule: monthly deliveries.
 - adult coverage open vials are 1-1 with facility visits because all adult coverage tracks is tetanus.  This 
    exploits that and simply does a join to get adult_coverage_tetanus_vials_opened in reports.  So not useful outside 
    of Adult Coverage with just Tetanus.

"""


FIELD_MAP = 'fieldmap.csv'
DB_NAME = 'open_lmis'
DB_HOST = 'localhost'
DB_PORT = '5432'
DB_USER = "postgres"

# table names
ADULT_COVERAGE_TABLE = 'vaccination_adult_coverage_line_items'
ADULT_COVERAGE_OPEN_VIAL_TABLE = 'adult_coverage_opened_vial_line_items'
CHILD_COVERAGE_TABLE = 'vaccination_child_coverage_line_items'
CHILD_COVERAGE_OPEN_VIAL_TABLE = 'child_coverage_opened_vial_line_items'
DZ_TABLE = 'delivery_zones'
DISTRIBUTION_TABLE = 'distributions'
EPI_INV_TABLE = 'epi_inventory_line_items'
EPI_USE_TABLE = 'epi_use_line_items'
FACILITY_TABLE = 'facilities'
FACILITY_VISIT_TABLE = 'facility_visits'
FACILITY_VISIT_REPORT_TABLE = 'facility_visits_report'
FULL_COVERAGE_TABLE = 'full_coverages'
GEO_ZONE_TABLE = 'geographic_zones'
GEO_LEVEL_TABLE = 'geographic_levels'
PERIOD_TABLE = 'processing_periods'
PRODUCT_TABLE = 'products'
PRODUCT_GROUP_TABLE = 'product_groups'


# Geo level name=>code that visit report record wants
GEO_LEVEL = {'district': 'commune',
	     'province': 'dept' }


FACILITY_VISIT_SQL = """SELECT fv.id AS id
    , f.code || '-' || to_char(period.startdate, 'YYYY-MM') AS visit_code
    , fv.facilityid AS facility_id
    , fv.visited AS visited
    , fv.visitdate AS visited_date
    , '2010-06-01' AS visited_last_date
    , fv.confirmedbyname AS confirmed_by_name
    , fv.confirmedbytitle AS confirmed_by_title
    , fv.verifiedbyname AS verified_by_name
    , fv.verifiedbytitle AS verified_by_title
    , fv.reasonfornotvisiting AS no_visit_reason
    , fv.otherreasondescription AS no_visit_other_reason
    , fv.observations
    , fv.facilitycatchmentpopulation AS catchement_population
    , period.id as period_id
    , dz.id AS delivery_zone_id
    , fc.femalehealthcenter AS full_vaccinations_female_hc
    , fc.femaleoutreach AS full_vaccinations_female_mb
    , fc.malehealthcenter AS full_vaccinations_male_hc
    , fc.maleoutreach AS full_vaccinations_male_mb
    , acov.openedvials AS adult_coverage_tetanus_vials_opened
    FROM %(facilityVisitsTable)s AS fv
    JOIN %(facilitiesTable)s AS f ON (fv.facilityid=f.id)
    JOIN %(distributionsTable)s AS d ON (fv.distributionid=d.id)
    JOIN %(deliveryZonesTable)s AS dz on (d.deliveryzoneid=dz.id)
    JOIN %(periodsTable)s AS period ON (d.periodid=period.id)
    JOIN %(adultCovOpenVialTable)s AS acov ON (acov.facilityvisitid=fv.id)
    LEFT JOIN %(fullCoveragesTable)s AS fc ON (fc.facilityvisitid=fv.id)
    WHERE period.startdate >= '2014-04-01'""" % \
    {'facilityVisitsTable': FACILITY_VISIT_TABLE,
     'facilitiesTable': FACILITY_TABLE,
     'fullCoveragesTable':  FULL_COVERAGE_TABLE,
     'distributionsTable': DISTRIBUTION_TABLE,
     'deliveryZonesTable': DZ_TABLE,
     'periodsTable': PERIOD_TABLE,
     'adultCovOpenVialTable': ADULT_COVERAGE_OPEN_VIAL_TABLE}


GEO_ZONE_SQL = """ SELECT gz.id
    , gz.parentid
    , gl.code AS geo_level_code
    FROM %(geoZoneTable)s AS gz
    JOIN %(geoLevelTable)s AS gl ON (gz.levelid=gl.id)""" % \
    {'geoZoneTable': GEO_ZONE_TABLE, 'geoLevelTable': GEO_LEVEL_TABLE}


EPI_INV_DISTINCT_CODE_SQL = """SELECT DISTINCT epiln.productcode
    FROM %(epiInvTable)s AS epiln""" % \
    {'epiInvTable': EPI_INV_TABLE}


# Select all distinct epi inventory line item product group names
EPI_USE_DISTINCT_CODE_SQL = """SELECT DISTINCT pg.code
    FROM %(epiUseTable)s AS euln
    JOIN %(productGroupTable)s AS pg ON (euln.productgroupid=pg.id)""" % \
    {'epiUseTable': EPI_USE_TABLE, 
    'productGroupTable': PRODUCT_GROUP_TABLE}


# Select all epi use line items and associated product group code
EPI_USE_LINE_ITEM_SQL = """SELECT euli.facilityvisitid
    , pg.code AS product_code
    , euli.stockatfirstofmonth AS first_of_month
    , euli.stockatendofmonth AS end_of_month
    , euli.expirationdate AS expiration
    , euli.received
    , euli.distributed
    , euli.loss
    FROM %(epiUseTable)s AS euli
    JOIN %(productGroupTable)s AS pg ON (euli.productgroupid=pg.id)""" % \
    {'epiUseTable': EPI_USE_TABLE, 
     'productGroupTable': PRODUCT_GROUP_TABLE}


# select distinct demographic group names as used in the adult coverage line item table
ADULT_COV_DISTINCT_GROUP_SQL = """SELECT DISTINCT demographicgroup
    FROM %(adultCovTable)s""" % \
    {'adultCovTable': ADULT_COVERAGE_TABLE}


# select distinct vaccination (e.g. BCG, MEASLES) as used in child coveage line item table
CHILD_COV_DISTINCT_VACC_SQL = """SELECT DISTINCT vaccination
    FROM %(childCovTable)s""" % \
    {'childCovTable': CHILD_COVERAGE_TABLE}


# select distinct product vial names from the child coverage opened vial table
CHILD_COV_DISTINCT_PRODUCT_VIAL_SQL = """SELECT DISTINCT productvialname
    FROM %(childCovOpenVialTable)s""" % \
    {'childCovOpenVialTable': CHILD_COVERAGE_OPEN_VIAL_TABLE}


def loadRefTable(cursor, tableName):
    cursor.execute("select * from %(table)s" % {'table': tableName})
    return cursor.fetchall()


def toUtf(string):
    """
    Encodes a string into utf-8.  Returns same object if not a string or is already a unicode object.
    No attempt is made to determine/convert a unicode object is made.

    @param string: a string to convert to unicode using the utf-8 encoding
    @return a unicode object using utf-8. Or the same object as passed if the string parameter wasn't a
	subtype of basestring or was already a subtype of unicode.

    """

    if not isinstance(string, types.StringTypes): return string
    if isinstance(string, unicode): return string
    try:
	asUtf8 = unicode(string, 'utf-8')
    except:
	print 'Failure to encode as utf-8: ' + string
	print type(string)
	raise
    return asUtf8


def rowToTable(rowData, keyColumn, allowDupes = False):
    """
    Turns a list of dict's into a dict that's keyed off one common column from the dicts.
    Return: a dict keyed off of keyColumn found in all rowData.  If multiple row's would
    be keyed off the same value, then each item from rowData will be in a list under the same key.
    """
    table = dict()
    for row in rowData:
	if keyColumn not in row:
	    raise LookupError('Key column ' + keyColumn + ' not in row data: ' + str(rowData))
	key = toUtf(row[keyColumn])
	if allowDupes == False and row[keyColumn] in table:
	    raise StandardError('Duplicate key ' + row[keyColumn] + ' found')

	# turn row dict into a dict with strings in utf
	asDict = {}
	for k,v in row.iteritems():
	    if isinstance(v, basestring): v = toUtf(v)
	    asDict[toUtf(k)] = v

	# enter dict item into result dict that's keyed off the column given.  If
	# an item already exists under that key, turn the value into a list of dicts.
	if key in table:
	    if not isinstance(table[key], list): table[key] = [table[key],]
	    table[key].append(asDict)
	else: table[key] = asDict

    return table


def loadFields():
    """
    Loads field names we want from field map file
    Return: list of field names
    """
    fieldNames = []
    f = open(FIELD_MAP, mode='rb')
    reader = csv.DictReader(f)
    for rowD in reader:
	fieldNames.append(rowD['fieldname'])

    f.close()
    return fieldNames


def loadFacilityVisits(conn):
    """
    Given a db connection, will fetch the OpenLMIS facility visit data.
    Return: an Iterable with facility visit data where every element is a 
    row of dicts keyed off of the column name.  Data is sorted by period's 
    start date ascending.
    """
    return loadAllFromSql(conn, FACILITY_VISIT_SQL)


def loadEpiUseLineItems(conn):
    epiUseRows = loadAllFromSql(conn, EPI_USE_LINE_ITEM_SQL)
    # convert expiration date from string in db to datetime, not done in db to avoid db setting for datestyle
    for row in epiUseRows:
	if 'expiration' in row and row['expiration'] is not None:
	    row['expiration'] = datetime.strptime(row['expiration'], '%m/%Y').date()
    
    return epiUseRows


def loadAllFromSql(conn, sql):
    """
    Fetches result (all) by running the given sql.
    @param conn an open db connection
    @param sql the sql string to run
    @return result as a dict
    """

    loadCur = getDictCursor(conn)
    loadCur.execute(sql)
    lineItems = loadCur.fetchall()
    loadCur.close()
    return lineItems


def storeVisits(conn, visitRows, fields):
    if len(visitRows) == 0:
	return
    cur = conn.cursor()
    cur.execute( 'DELETE FROM ' + FACILITY_VISIT_REPORT_TABLE) # clear before new load

    # create a list of tuples that are the values from visits using the order and name of the fields
    # from fields
    valueTups = [tuple(visitD.get(fname) for fname in fields) for visitD in visitRows]
    colStr = '(' + ','.join(fname for fname in fields) + ')'

    # create a value string that's SQL safe for every tuple we created, this allows the insert
    # statement later to be one statement, one column list and then a list of all the values/rows
    formatStr= '(' + ','.join('%s' for fname in fields) + ')'
    valStr = ','.join(cur.mogrify(formatStr, tup) for tup in valueTups)
    insStr = 'INSERT INTO ' + FACILITY_VISIT_REPORT_TABLE + ' ' + colStr + ' VALUES ' + valStr 
    cur.execute(insStr)
    cur.close()


def loadGeoZone(cur):
    cur.execute(GEO_ZONE_SQL)
    return cur.fetchall()


def loadEpiInvDistinctCodes(conn):
    return loadDistinct(conn, EPI_INV_DISTINCT_CODE_SQL)


def loadEpiUseDistinctCodes(conn):
    return loadDistinct(conn, EPI_USE_DISTINCT_CODE_SQL)


def loadAdultCovDistinctGroups(conn):
    return loadDistinct(conn, ADULT_COV_DISTINCT_GROUP_SQL)


def loadChildCovDistinctVaccs(conn):
    return loadDistinct(conn, CHILD_COV_DISTINCT_VACC_SQL)


def loadChildCovDistinctProductVialNames(conn):
    return loadDistinct(conn, CHILD_COV_DISTINCT_PRODUCT_VIAL_SQL)


def loadDistinct(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    asList = [row[0] for row in rows]
    return asList


def loadOpenLmis(conn):
    """
    Loads and processes OpenLMIS facility visits to final reporting form.
    @param conn: open database connection
    @return list of OpenLMIS facility visits in reporting form.  Each entry in the list represents
    one facility visit as a dict whose keys conform to the reporting columns.
    
    """
    if conn is None:
	raise Exception("data source connection is not active")
    cur = getDictCursor(conn)

    facilityTable = rowToTable( loadRefTable(cur, FACILITY_TABLE), 'id' )
    geoZoneTable = rowToTable( loadGeoZone(cur), 'id' )

    # adds all geozones by geo level to all facilities
    map(lambda f: facilityAddGeoLevels(f, geoZoneTable), facilityTable.values())

    # load facility visit's
    facVisitRows = loadFacilityVisits(conn)

    # add geographic_zone id's for the levels we're interested in for every facility visit
    geoKeys = [geoPrefix + '_id' for geoPrefix in GEO_LEVEL]
    map(lambda fv: dictColCopy(facilityTable[fv['facility_id']], fv, geoKeys), facVisitRows)

    # load and map epi_inventory columns for every facility visit
    epiInvTable = rowToTable( loadRefTable(cur, EPI_INV_TABLE), 'facilityvisitid', allowDupes=True)
    epiInvProdCodes = loadEpiInvDistinctCodes(conn)
    mapEpiInvToFacVisits(facVisitRows, epiInvTable, epiInvProdCodes)

    # load and map epi_use columns for every facility visit
    epiUseTable = rowToTable( loadEpiUseLineItems(conn), 'facilityvisitid', allowDupes=True)
    epiUseProdCodes = loadEpiUseDistinctCodes(conn)
    mapEpiUseToFacVisits(facVisitRows, epiUseTable, epiUseProdCodes)

    # load and map adult coverage line items for every facility visit
    adultCovTable = rowToTable( loadRefTable(cur, ADULT_COVERAGE_TABLE), 'facilityvisitid', allowDupes=True)
    adultCovDemoGroups = loadAdultCovDistinctGroups(conn)
    mapAdultCoverageToFacVisits(facVisitRows, adultCovTable, adultCovDemoGroups)

    # load and map child coverage line items for every facility visit
    childCovTable = rowToTable( loadRefTable(cur, CHILD_COVERAGE_TABLE), 'facilityvisitid', allowDupes=True)
    childCovVaccs = loadChildCovDistinctVaccs(conn)
    mapChildCoverageToFacVisits(facVisitRows, childCovTable, childCovVaccs)

    # load and map child coverage opened vial line items for every facility visit
    childCovOpenVialTable = rowToTable( loadRefTable(cur, CHILD_COVERAGE_OPEN_VIAL_TABLE), 'facilityvisitid', allowDupes=True)
    childCovProductVialNames = loadChildCovDistinctProductVialNames(conn)
    mapChildCoverageOpenVialsToFacVisits(facVisitRows, childCovOpenVialTable, childCovProductVialNames)

    cur.close()
    return facVisitRows


def generateLastVisitDate(visitRows):
    """
    Generates the visited_last_date field for every record given by searching through all records to find
    the last visit date for each facility.  Sets visited_last_date field to None if there was no last visit,
    or the last visit date regardless of weather the facility record indicates it was not visited or not recorded.
    
    @param visitRows a list of dicts that is the entire visit record history and that will be updated with the 
	visited_last_date field.

    """

    # sort chronological ascending by visit_code which will group by facility and then period (e.g. 2013-03, 2013-04)
    rowsAsc = sorted(visitRows, key=lambda r: r['visit_code'])

    # for every visit row, extract facility code, enter it into map where fac_code => last_visit_date
    lastVisitMap = dict()
    for row in rowsAsc:
	mapKey = row['facility_id']

	# a) if no record in map exists, or value is None, last visit date for record is None
	# b) if record in map exisits and is not None, last visit date for record is map value
	# c) update map value with row's visit_date field if row's visit date is not None (i.e. only update for visits)
	mapValue = lastVisitMap.get(mapKey)
	row['visited_last_date'] = mapValue
	if row['visited_date'] is not None:
	    lastVisitMap[mapKey] = row['visited_date']


def geoZoneFlatten(geoZone, geoZoneTable):
    """
    Given a leaf geographic zone, will loop up to each geographic zone parent until the heirachy is flat.
    Returns a dict that's keyed off of the geographic level code
    """
    gz = {}
    while geoZone is not None:
	gz[geoZone['geo_level_code']] = geoZone
	geoZone = geoZoneTable.get(geoZone['parentid'])

    return gz


def facilityAddGeoLevels(fac, geoZoneTable):
    """
    Updates a facility dictionary in place with key/values from the geographic zone's the facility is in.
    Uses GEO_LEVEL to only add zone's that are at those levels and adds keys prepended with the geographic level
    name.
    """
    geoZoneFirst = geoZoneTable.get(fac['geographiczoneid'])
    geoFlat = geoZoneFlatten(geoZoneFirst, geoZoneTable)
    for geoLevelName, geoLevelCode in GEO_LEVEL.iteritems():
	geoLevel = geoFlat.get(geoLevelCode)
	if geoLevel is None:
	    raise Exception('GeoLevelCode ' + geoLevelCode + ' not in geographic level table')
	for geoK, geoV in geoLevel.iteritems():
	    fac[geoLevelName + '_' + geoK] = geoV

    return fac # allows inline use


def dictColCopy(fromDict, toDict, keyList):
    """
    Copies keys specified in keyList from the fromDict to the newDict.
    """

    for key in keyList:
	if key not in fromDict:
	    raise Exception('Key ' + key + ' not found in: ' + str(fromDict))
	toDict[key] = fromDict[key]


def getDictCursor(conn):
    """
    Gets a dictionary cursor from the db conn
    """
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def mapEpiInvToFacVisits(facVisitRows, epiInvTable, epiInvProdCodes):
    """
    Given a list of the facility visits, the epi inventory line items as a table, 
    and a list of distinct epi product codes, will map those epi inventory columns 
    into the facility visit row.
    """

    cols = ['existingquantity', 'spoiledquantity', 'deliveredquantity', 'idealquantity']
    keyColName = 'productcode'
    
    # pivot the line items into a column structure
    def rename(origColName): # a function that the pivoted column names will be renamed with
	newColName = re.sub(r'quantity', '', origColName)
	newColName = re.sub(r'ideal', 'isa', newColName)
	newColName = re.sub(r'bcg20', 'bcg', newColName)
	newColName = re.sub(r'measles10', 'measles', newColName)
	newColName = re.sub(r'tetanus10', 'tetanus', newColName)
	newColName = re.sub(r'yfv1', 'hpv2', newColName)
	return 'epi_inventory_' + newColName
    mapLineItemsToFacVisits(facVisitRows, epiInvTable, keyColName, epiInvProdCodes, cols, rename)


def mapEpiUseToFacVisits(facVisitRows, epiUseTable, epiUseProdCodes):
    """
    Given a list of the facility visits, the epi use line items as a table, 
    and a list of distinct epi product codes, will map those epi line items to columns 
    into the facility visit row.
    """
    
    cols = ['first_of_month', 'received', 'distributed', 'loss', 'end_of_month', 'expiration']
    keyColName = 'product_code'
    def rename(origColName): # a function that the pivoted column names will be renamed with
	newColName = re.sub('1bcg', 'bcg', origColName)
	newColName = re.sub('2bcgdil', 'bcgdil', newColName)
	newColName = re.sub('2polio', 'polio', newColName)
	newColName = re.sub('3penta', 'penta', newColName)
	newColName = re.sub('5measles', 'measles', newColName)
	newColName = re.sub('6measlesdil', 'measlesdil', newColName)
	newColName = re.sub('4pcv10', 'pcv', newColName)
	newColName = re.sub('6yfv', 'hpv', newColName)
	newColName = re.sub('8tetanus', 'tetanus', newColName)
	return 'epi_use_' + newColName
    mapLineItemsToFacVisits(facVisitRows, epiUseTable, keyColName, epiUseProdCodes, cols, rename)


def mapAdultCoverageToFacVisits(facVisitRows, adultCovTable, adultCovDemographicGroups):
    """
    Given a list of the facility visits, the adult vaccination line items as a table, 
    and a list of distinct adult coverage demographic groups, will map those items to columns 
    into the facility visit row.
    """
    
    cols = ['healthcentertetanus1', 'outreachtetanus1', 'healthcentertetanus2to5', 'outreachtetanus2to5', 'targetgroup']
    keyColName = 'demographicgroup'
    def rename(origColName): # a function that the pivoted column names will be renamed with
	newColName = re.sub('MIF 15-49 years - Students', 'mif_student', origColName)
	newColName = re.sub('MIF 15-49 years - Community', 'mif_community', newColName)
	newColName = re.sub('MIF 15-49 years - Workers', 'mif_worker', newColName)
	newColName = re.sub('Students not MIF', 'student', newColName)
	newColName = re.sub('Other not MIF', 'other', newColName)
	newColName = re.sub('Workers not MIF', 'worker', newColName)
	newColName = re.sub('Pregnant Women', 'pregnant', newColName)
	newColName = re.sub('healthcentertetanus1', 'tetanus1hc', newColName)
	newColName = re.sub('outreachtetanus1', 'tetanus1mb', newColName)
	newColName = re.sub('healthcentertetanus2to5', 'tetanus25hc', newColName)
	newColName = re.sub('outreachtetanus2to5', 'tetanus25mb', newColName)
	newColName = re.sub('targetgroup', 'target_group', newColName)
	return 'adult_coverage_' + newColName
    mapLineItemsToFacVisits(facVisitRows, adultCovTable, keyColName, adultCovDemographicGroups, cols, rename)


def mapChildCoverageToFacVisits(facVisitRows, childCovTable, childCovVaccs):
    """
    Given a list of the facility visits, the child coverage line items as a table, 
    and a list of distinct child coverage vaccinations, will map those line items to columns 
    into the facility visit row.
    """
    
    cols = ['healthcenter11months', 'outreach11months', 'healthcenter23months', 'outreach23months', 'targetgroup']
    keyColName = 'vaccination'
    def rename(origColName): # a function that the pivoted column names will be renamed with
	newColName = re.sub('BCG', 'bcg', origColName)
	newColName = re.sub('Measles', 'measles', newColName)
	newColName = re.sub('PCV10 ', 'pcv', newColName)
	newColName = re.sub('Penta ', 'penta', newColName)
	newColName = re.sub('Polio ', 'polio', newColName)
	newColName = re.sub('\(Newborn\)', '0', newColName)
	newColName = re.sub('1st dose', '1', newColName)
	newColName = re.sub('2nd dose', '2', newColName)
	newColName = re.sub('3rd dose', '3', newColName)
	newColName = re.sub('healthcenter', 'hc', newColName)
	newColName = re.sub('outreach', 'mb', newColName)
	newColName = re.sub('11months', '0_11', newColName)
	newColName = re.sub('23months', '12_23', newColName)
	newColName = re.sub('(.+)\d+_targetgroup', r'\1_targetgroup', newColName) # fix things like polio2_targetgroup to be just polio_targetgroup
	newColName = re.sub('targetgroup', 'target_group', newColName)
	return 'child_coverage_' + newColName
    mapLineItemsToFacVisits(facVisitRows, childCovTable, keyColName, childCovVaccs, cols, rename)


def mapChildCoverageOpenVialsToFacVisits(facVisitRows, childCovOpenVialsTable, childCovProductVialNames):
    """
    Given a list of the facility visits, the child coverage opened vial line items as a table, 
    and a list of distinct child coverage product vial names, will map those line items to columns 
    into the facility visit row.
    """
    
    cols = ['openedvials']
    keyColName = 'productvialname'
    def rename(origColName): # a function that the pivoted column names will be renamed with
	newColName = re.sub('BCG', 'bcg', origColName)
	newColName = re.sub('Measles', 'measles', newColName)
	newColName = re.sub('PCV', 'pcv', newColName)
	newColName = re.sub('Penta', 'penta', newColName)
	newColName = re.sub('Polio', 'polio', newColName)
	newColName = re.sub('openedvials', 'vials_opened', newColName)
	return 'child_coverage_' + newColName
    mapLineItemsToFacVisits(facVisitRows, childCovOpenVialsTable, keyColName, childCovProductVialNames, cols, rename)


def mapLineItemsToFacVisits(facVisitRows, lineItemTable, keyColName, distinctCodeList, desiredCols, colRenameFn):
    """
    Transforms line items to a column structure, maps each of those back to appropriate facility visit row.

    @param facVisitRows: a list of dicts where each dict is a facility visit.  Each dict will have the keys & values 
	from the line item pivot added to it.
    @param lineItemTable: the line items to pivot as a dict of either one dict or a list of dict.  They key
	should map the line item(s) to the facility visit row by facVisitRows['id']
    @param keyColName: the name of the column the pivot function will pivot around found in every entry of the 
	lineItemTable
    @param distinctCodeList: a set of values we will use to maintain a consistent number of columns throughout
	the pivot of each line items to facility visits.  Values here are found in the line items keyColName
    @param desiredCols: a set of columns found in every line item that we want to turn into columns
    @param colRenameFn: a function that takes the name of the original column from desiredCols and maps it to
	a new name that will be found in the resulting map

    """
    # pivot the line items to a col structure
    pivot = pivotLineItems(lineItemTable, keyColName, distinctCodeList, desiredCols, colRename = colRenameFn)
    
    def copyLineItemsToFacVisit(facVisitD):
	fvId = facVisitD['id']
	dictColCopy(pivot[fvId], facVisitD, pivot[fvId].keys())
    map(copyLineItemsToFacVisit, facVisitRows) 
    

def pivotLineItems(itemTable, keyColName, keyColValues, columns, colRename = None):
    """
    Given an item table of the form:
	key => lineItem or
	key => list(lineItems)
    return a dict of the form:
	key => lineItemDict
    Where lineItemDict has keys of the form:
	"keyColValue_column" => lineItem[column] for lineItem[keyColName]

    e.g.
    keyColName: 'code'
    keyColValues: ['AAA', 'BBB']
    columns: ['existing']
    lineItems: [ {'code': 'AAA', 'existing': 10},
		 {'code': 'BBB', 'existing': 20} ]
    lineItemDict: {'AAA_existing': 10, 'BBB_existing': 20}

    If colRename is defined, it must be a function that takes a key string and returns a key string
    that will be used in place of keyColValue_column. e.g. {colRename('AAA_existing'): 10}
    """

    if columns is None: return itemTable
    
    # loop through line item table, for every key we look at the line items for that key
    pivotD = {} # holds the resulting pivot table
    for liKey, liValue in itemTable.iteritems():
	liDict = {}
	if not isinstance(liValue, list): liValue = [liValue,]
	liAsTable = rowToTable(liValue, keyColName)

	# look through every combination of given key column values and given columns,
	# constructing a dict that has columns of keyColumnValue_columns
	for kcVal in keyColValues:
	    for col in columns:
		lineItem = liAsTable.get(kcVal)
		theVal = lineItem.get(col) if lineItem is not None else None
		colName = kcVal + '_' + col
		if colRename is not None: 
		    colName = colRename(colName)
		liDict[colName] = theVal
		
	pivotD[liKey] = liDict

    return pivotD


def printMissingFieldNames(visitRows, fieldNames):
    for row in visitRows:
	for fname in fieldNames:
	    if fname not in row:
		print 'Missing field name: ' + fname + ' from row with key: ' + row['visit_code']
 

dbConn = None
try:
    dbConn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER)

    # load open lmis facility visit rows
    facVisitRows = loadOpenLmis(dbConn)

    # generate last visit date for every record
    generateLastVisitDate(facVisitRows)

    # load desired field list and store all visit rows in report DB
    fields = loadFields()
    #printMissingFieldNames(facVisitRows, fields)
    storeVisits(dbConn, facVisitRows, fields)

    dbConn.commit()

    print "distributions-etl has completed"
except BaseException, err:
    if dbConn is not None:
	dbConn.rollback()
    raise 
finally:
    if dbConn is not None:
	dbConn.close()
