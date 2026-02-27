
#This code looks at a DEM raster to find the dimensions, then writes a script to create a STRM raster.

import os, subprocess

import numpy as np
from osgeo import gdal
import matplotlib.pyplot as plt
from numba import njit

from .logger import LOG

def convert_cell_size(dem_cell_size, dem_lower_left, dem_upper_right):
    """
    Determines the x and y cell sizes based on the geographic location

    Parameters
    ----------
    None. All input data is available in the parent object

    Returns
    -------
    None. All output data is set into the object

    """

    ### Get the cell size ###
    d_lat = np.fabs((dem_lower_left + dem_upper_right) / 2)

    ### Determine if conversion is needed
    if dem_cell_size > 0.5:
        # This indicates that the DEM is projected, so no need to convert from geographic into projected.
        x_cell_size = dem_cell_size
        y_cell_size = dem_cell_size
        projection_conversion_factor = 1

    else:
        # Reprojection from geographic coordinates is needed
        assert d_lat > 1e-16, "Please use lat and long values greater than or equal to 0."

        # Determine the latitude range for the model
        if d_lat >= 0 and d_lat <= 10:
            d_lat_up = 110.61
            d_lat_down = 110.57
            d_lon_up = 109.64
            d_lon_down = 111.32
            d_lat_base = 0.0

        elif d_lat > 10 and d_lat <= 20:
            d_lat_up = 110.7
            d_lat_down = 110.61
            d_lon_up = 104.64
            d_lon_down = 109.64
            d_lat_base = 10.0

        elif d_lat > 20 and d_lat <= 30:
            d_lat_up = 110.85
            d_lat_down = 110.7
            d_lon_up = 96.49
            d_lon_down = 104.65
            d_lat_base = 20.0

        elif d_lat > 30 and d_lat <= 40:
            d_lat_up = 111.03
            d_lat_down = 110.85
            d_lon_up = 85.39
            d_lon_down = 96.49
            d_lat_base = 30.0

        elif d_lat > 40 and d_lat <= 50:
            d_lat_up = 111.23
            d_lat_down = 111.03
            d_lon_up = 71.70
            d_lon_down = 85.39
            d_lat_base = 40.0

        elif d_lat > 50 and d_lat <= 60:
            d_lat_up = 111.41
            d_lat_down = 111.23
            d_lon_up = 55.80
            d_lon_down = 71.70
            d_lat_base = 50.0

        elif d_lat > 60 and d_lat <= 70:
            d_lat_up = 111.56
            d_lat_down = 111.41
            d_lon_up = 38.19
            d_lon_down = 55.80
            d_lat_base = 60.0

        elif d_lat > 70 and d_lat <= 80:
            d_lat_up = 111.66
            d_lat_down = 111.56
            d_lon_up = 19.39
            d_lon_down = 38.19
            d_lat_base = 70.0

        elif d_lat > 80 and d_lat <= 90:
            d_lat_up = 111.69
            d_lat_down = 111.66
            d_lon_up = 0.0
            d_lon_down = 19.39
            d_lat_base = 80.0

        else:
            raise AttributeError('Please use legitimate (0-90) lat and long values.')

        ## Convert the latitude ##
        d_lat_conv = d_lat_down + (d_lat_up - d_lat_down) * (d_lat - d_lat_base) / 10
        y_cell_size = dem_cell_size * d_lat_conv * 1000.0  # Converts from degrees to m

        ## Longitude Conversion ##
        d_lon_conv = d_lon_down + (d_lon_up - d_lon_down) * (d_lat - d_lat_base) / 10
        x_cell_size = dem_cell_size * d_lon_conv * 1000.0  # Converts from degrees to m

        ## Make sure the values are in bounds ##
        if d_lat_conv < d_lat_down or d_lat_conv > d_lat_up or d_lon_conv < d_lon_up or d_lon_conv > d_lon_down:
            raise ArithmeticError("Problem in conversion from geographic to projected coordinates")

        ## Calculate the conversion factor ##
        projection_conversion_factor = 1000.0 * (d_lat_conv + d_lon_conv) / 2.0
    return x_cell_size, y_cell_size, projection_conversion_factor

def FindFlowRateForEachCOMID(FlowFileName, COMID_Unique, COMID_to_ID, MinCOMID, MaxCOMID):
    num_unique = len(COMID_Unique)
    COMID_Unique_Flow = np.zeros(num_unique)
    LOG.info('\nOpening and Reading ' + FlowFileName)
    infile = open(FlowFileName,'r')
    lines = infile.readlines()
    infile.close()
    
    num_lines = len(lines)
    for n in range(1,num_lines):
        (COMID, Q) = lines[n].strip().split(',')
        if int(COMID)>=MinCOMID and int(COMID)<=MaxCOMID:
            i = COMID_to_ID[int(COMID)-MinCOMID]
            COMID_Unique_Flow[i] = float(Q)
    return COMID_Unique_Flow

def Calculate_TW_D_ForEachCOMID(CurveParamFileName, COMID_Unique_Flow, COMID_Unique, COMID_to_ID, MinCOMID, Q_Fraction):
    num_unique = len(COMID_Unique)
    COMID_Unique_TW = np.zeros(num_unique)
    COMID_Unique_Depth = np.zeros(num_unique)
    COMID_NumRecord = np.zeros(num_unique)
    LOG.info('\nOpening and Reading ' + CurveParamFileName)
    infile = open(CurveParamFileName,'r')
    lines = infile.readlines()
    infile.close()
    
    num_lines = len(lines)
    for n in range(1,num_lines):
        (COMID, R, C, E, QB, MF, Dbv, Da, Db, Vbv, Va, Vb, Tbv, Ta, Tb) = lines[n].strip().split(',')
        i = COMID_to_ID[int(COMID)-MinCOMID]
        if float(Da)>0.0 and float(Db)>0.0 and float(Ta)>0.0 and float(Tb)>0.0:
            COMID_NumRecord[i] = COMID_NumRecord[i] + 1
            Q = COMID_Unique_Flow[i] * Q_Fraction
            Depth = float(Dbv) + float(Da) * pow(Q,float(Db))
            TopWidth = float(Tbv) + float(Ta) * pow(Q,float(Tb))
            
            #Calculate the Average Depth and TopWidth
            COMID_Unique_TW[i] = ( COMID_Unique_TW[i]*(COMID_NumRecord[i]-1) + TopWidth ) / COMID_NumRecord[i]
            COMID_Unique_Depth[i] = ( COMID_Unique_Depth[i]*(COMID_NumRecord[i]-1) + Depth ) / COMID_NumRecord[i]
    TopWidthMax = COMID_Unique_TW.max()
    return (COMID_Unique_TW, COMID_Unique_Depth, TopWidthMax)

def Calculate_TW_D_ForEachCOMID_ARC(CurveParamFileName, COMID_Unique_Flow, COMID_Unique, COMID_to_ID, MinCOMID, MaxCOMID, Q_Fraction):
    num_unique = len(COMID_Unique)
    COMID_Unique_TW = np.zeros(num_unique)
    COMID_Unique_Depth = np.zeros(num_unique)
    COMID_NumRecord = np.zeros(num_unique)
    LOG.info('\nOpening and Reading ' + CurveParamFileName)
    infile = open(CurveParamFileName,'r')
    lines = infile.readlines()
    infile.close()
    
    num_lines = len(lines)
    for n in range(1,num_lines):
        (COMID, R, C, Base_E, DEM_E, QMax, Slope, Da, Db, Ta, Tb, Va, Vb) = lines[n].strip().split(',')
        if int(COMID)>=MinCOMID and int(COMID)<=MaxCOMID:
            i = COMID_to_ID[int(COMID)-MinCOMID]
            if float(Da)>0.0 and float(Db)>0.0 and float(Ta)>0.0 and float(Tb)>0.0:
                COMID_NumRecord[i] = COMID_NumRecord[i] + 1
                Q = COMID_Unique_Flow[i] * Q_Fraction
                Depth = float(Da) * pow(Q,float(Db))
                TopWidth = float(Ta) * pow(Q,float(Tb))
                
                #Calculate the Average Depth and TopWidth
                COMID_Unique_TW[i] = ( COMID_Unique_TW[i]*(COMID_NumRecord[i]-1) + TopWidth ) / COMID_NumRecord[i]
                COMID_Unique_Depth[i] = ( COMID_Unique_Depth[i]*(COMID_NumRecord[i]-1) + Depth ) / COMID_NumRecord[i]
    TopWidthMax = COMID_Unique_TW.max()
    LOG.info('Max TopWidth is ' + str(TopWidthMax))
    return (COMID_Unique_TW, COMID_Unique_Depth, TopWidthMax)

    

def Get_Raster_Details(DEM_File):
    LOG.info(DEM_File)
    gdal.Open(DEM_File, gdal.GA_ReadOnly)
    data = gdal.Open(DEM_File)
    geoTransform = data.GetGeoTransform()
    ncols = int(data.RasterXSize)
    nrows = int(data.RasterYSize)
    minx = geoTransform[0]
    dx = geoTransform[1]
    maxy = geoTransform[3]
    dy = geoTransform[5]
    maxx = minx + dx * ncols
    miny = maxy + dy * nrows
    Rast_Projection = data.GetProjectionRef()
    data = None
    return minx, miny, maxx, maxy, dx, dy, ncols, nrows, geoTransform, Rast_Projection

def Read_Raster_GDAL(InRAST_Name):
    dataset = gdal.Open(InRAST_Name, gdal.GA_ReadOnly)     
    # Retrieve dimensions of cell size and cell count then close DEM dataset
    geotransform = dataset.GetGeoTransform()
    # Continue grabbing geospatial information for this use...
    band = dataset.GetRasterBand(1)
    RastArray = band.ReadAsArray()
    #global ncols, nrows, cellsize, yll, yur, xll, xur
    ncols=band.XSize
    nrows=band.YSize
    band = None
    cellsize = geotransform[1]
    yll = geotransform[3] - nrows * np.fabs(geotransform[5])
    yur = geotransform[3]
    xll = geotransform[0];
    xur = xll + (ncols)*geotransform[1]
    lat = np.fabs((yll+yur)/2.0)
    Rast_Projection = dataset.GetProjectionRef()
    dataset = None
    LOG.info('Spatial Data for Raster File:')
    LOG.info('   ncols = ' + str(ncols))
    LOG.info('   nrows = ' + str(nrows))
    LOG.info('   cellsize = ' + str(cellsize))
    LOG.info('   yll = ' + str(yll))
    LOG.info('   yur = ' + str(yur))
    LOG.info('   xll = ' + str(xll))
    LOG.info('   xur = ' + str(xur))
    return RastArray, ncols, nrows, cellsize, yll, yur, xll, xur, lat, geotransform, Rast_Projection

def GetListOfDEMs(inputfolder):
    DEM_Files = []
    for file in os.listdir(inputfolder):
        #if file.startswith('return_') and file.endswith('.geojson'):
        if file.endswith('.tif') or file.endswith('.img'):
            DEM_Files.append(file)
    return DEM_Files

def GetStreamlineBaseName(StreamShapefile):
    SFile = StreamShapefile.replace('\\','/')
    S_split = SFile.split('/')
    StrmBase = S_split[-1]
    StrmBase = StrmBase.replace('.shp','')
    return StrmBase

def Write_Output_Raster(s_output_filename, raster_data, ncols, nrows, dem_geotransform, dem_projection, s_file_format, s_output_type):   
    o_driver = gdal.GetDriverByName(s_file_format)  #Typically will be a GeoTIFF "GTiff"
    #o_metadata = o_driver.GetMetadata()
    
    # Construct the file with the appropriate data shape
    o_output_file = o_driver.Create(s_output_filename, xsize=ncols, ysize=nrows, bands=1, eType=s_output_type)

    # Get the first band (assuming a single-band raster)
    band = o_output_file.GetRasterBand(1)

    # Initialize the band with zeros
    band.Fill(0)

    # Write to disk
    band.FlushCache()
    
    # Set the geotransform
    o_output_file.SetGeoTransform(dem_geotransform)
    
    # Set the spatial reference
    o_output_file.SetProjection(dem_projection)
    
    # Write the data to the file
    o_output_file.GetRasterBand(1).WriteArray(raster_data)
    
    # Once we're done, close properly the dataset
    o_output_file = None


def Clean_STRM_Raster(STRM_File, STRM_File_Clean):
    LOG.info('\nCleaning up the Stream File.')
    (SN, ncols, nrows, cellsize, yll, yur, xll, xur, lat, dem_geotransform, dem_projection) = Read_Raster_GDAL(STRM_File)
    
    #Create an array that is slightly larger than the STRM Raster Array
    B = np.zeros((nrows+2,ncols+2))
    
    #Imbed the STRM Raster within the Larger Zero Array
    B[1:(nrows+1), 1:(ncols+1)] = SN
    
    #Added this because sometimes the non-stream values end up as -9999
    B = np.where(B>0,B,0)
    #(RR,CC) = B.nonzero()
    (RR,CC) = np.where(B>0)
    num_nonzero = len(RR)
    
    for filterpass in range(2):
        #First pass is just to get rid of single cells hanging out not doing anything
        p_count = 0
        p_percent = (num_nonzero+1)/100.0
        n=0
        for x in range(num_nonzero):
            if x>=p_count*p_percent:
                p_count = p_count + 1
                LOG.info(' ' + str(p_count), end =" ")
            r=RR[x]
            c=CC[x]
            V = B[r,c]
            if V>0:
                #Left and Right cells are zeros
                if B[r,c+1]==0 and B[r,c-1]==0:
                    #The bottom cells are all zeros as well, but there is a cell directly above that is legit
                    if (B[r+1,c-1]+B[r+1,c]+B[r+1,c+1])==0 and B[r-1,c]>0:
                        B[r,c] = 0
                        n=n+1
                    #The top cells are all zeros as well, but there is a cell directly below that is legit
                    elif (B[r-1,c-1]+B[r-1,c]+B[r-1,c+1])==0 and B[r+1,c]>0:
                        B[r,c] = 0
                        n=n+1
                #top and bottom cells are zeros
                if B[r,c]>0 and B[r+1,c]==0 and B[r-1,c]==0:
                    #All cells on the right are zero, but there is a cell to the left that is legit
                    if (B[r+1,c+1]+B[r,c+1]+B[r-1,c+1])==0 and B[r,c-1]>0:
                        B[r,c] = 0
                        n=n+1
                    elif (B[r+1,c-1]+B[r,c-1]+B[r-1,c-1])==0 and B[r,c+1]>0:
                        B[r,c] = 0
                        n=n+1
        LOG.info('\nFirst pass removed ' + str(n) + ' cells')
        
        
        #This pass is to remove all the redundant cells
        n=0
        p_count = 0
        p_percent = (num_nonzero+1)/100.0
        for x in range(num_nonzero):
            if x>=p_count*p_percent:
                p_count = p_count + 1
                LOG.info(' ' + str(p_count), end =" ")
            r=RR[x]
            c=CC[x]
            V = B[r,c]
            if V>0:
                if B[r+1,c]==V and (B[r+1,c+1]==V or B[r+1,c-1]==V):
                    if sum(B[r+1,c-1:c+2])==0:
                        B[r+1,c] = 0
                        n=n+1
                elif B[r-1,c]==V and (B[r-1,c+1]==V or B[r-1,c-1]==V):
                    if sum(B[r-1,c-1:c+2])==0:
                        B[r-1,c] = 0
                        n=n+1
                elif B[r,c+1]==V and (B[r+1,c+1]==V or B[r-1,c+1]==V):
                    if sum(B[r-1:r+1,c+2])==0:
                        B[r,c+1] = 0
                        n=n+1
                elif B[r,c-1]==V and (B[r+1,c-1]==V or B[r-1,c-1]==V):
                    if sum(B[r-1:r+1,c-2])==0:
                            B[r,c-1] = 0
                            n=n+1
        LOG.info('\nSecond pass removed ' + str(n) + ' redundant cells')
        
        #This pass is to remove all the redundant cells that may be connected to a different stream
        n=0
        p_count = 0
        p_percent = (num_nonzero+1)/100.0
        for x in range(num_nonzero):
            if x>=p_count*p_percent:
                p_count = p_count + 1
                LOG.info(' ' + str(p_count), end =" ")
            r=RR[x]
            c=CC[x]
            V = B[r,c]
            if V>0:
                if B[r+1,c]>0 and (B[r+1,c+1]>0 or B[r+1,c-1]>0):
                    if sum(B[r+1,c-1:c+2])==0:
                        B[r+1,c] = 0
                        n=n+1
                elif B[r-1,c]>0 and (B[r-1,c+1]>0 or B[r-1,c-1]>0):
                    if sum(B[r-1,c-1:c+2])==0:
                        B[r-1,c] = 0
                        n=n+1
                elif B[r,c+1]>0 and (B[r+1,c+1]>0 or B[r-1,c+1]>0):
                    if sum(B[r-1:r+1,c+2])==0:
                        B[r,c+1] = 0
                        n=n+1
                elif B[r,c-1]>0 and (B[r+1,c-1]>0 or B[r-1,c-1]>0):
                    if sum(B[r-1:r+1,c-2])==0:
                            B[r,c-1] = 0
                            n=n+1
        LOG.info('\nThird pass removed ' + str(n) + ' redundant cells')
    
    LOG.info('Writing Output File ' + STRM_File_Clean)
    Write_Output_Raster(STRM_File_Clean, B[1:nrows+1,1:ncols+1], ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Int32)
    #return B[1:nrows+1,1:ncols+1], ncols, nrows, cellsize, yll, yur, xll, xur
    return

@njit(cache=True)
def FindStreamCharacteristics(E, B, COMID_Unique, COMID_to_ID, MinCOMID, RR, CC, num_comids):
    E_Min = np.full(num_comids, 99999999.9, dtype=np.float64)
    E_Max = np.zeros(num_comids, np.float64)
    E_Avg = np.zeros(num_comids, np.float64)
    N = np.zeros(num_comids, np.int64)    #Number of Cells

    num_strm_cells = len(RR)
    
    p_count = 0
    p_percent = (num_strm_cells+1)/100.0
    
    for x in range(num_strm_cells):
        if x>=p_count*p_percent:
            p_count = p_count + 1
        r=RR[x]
        c=CC[x]
        V = B[r,c]
        Elev = E[r,c]
        if V<=0:
            continue

        i = COMID_to_ID[(V-MinCOMID)]
        
        if Elev < E_Min[i]:
            E_Min[i] = Elev 
        if Elev > E_Max[i]:
            E_Max[i] = Elev 
        E_Avg[i] = E_Avg[i] + Elev/100.0
        N[i] += 1        
    
    for x in range(num_comids):
        i = COMID_to_ID[int(int(COMID_Unique[x])-MinCOMID)]

        E_Avg[i] = E_Avg[i]*100 / N[i]
        #L[i] = L[i] / 2.0  #This has to be average because we did a lot of double-counting of stream cells
    return E_Min, E_Avg, E_Max

@njit(cache=True)
def Find_SEED_and_CONNECTIONS(RR, CC, B, E_Min, E_Max, COMID_to_ID, MinCOMID, nrows, ncols, SEED_Val, CON_Val):
    # todo: doc string

    ### Define the placeholder arrays ###
    SEED_r = []
    SEED_c = []
    SEED_MinElev=[]
    SEED_MaxElev=[]

    ### Create the connection matrix ###
    SEEDCONNECT = np.zeros((nrows, ncols), dtype=np.int8)

    ### Perform initial calculations ###
    # Count the number of nonzero cells
    num_nonzero = len(RR)

    ### Loop and process cells ###
    for x in range(num_nonzero):
        # Get the location of the current cell
        r = RR[x]
        c = CC[x]

        # Count the number of cells in the nine cell window with the same stream id. The current cell is included in the count
        n_same = np.count_nonzero(B[r-1:r+2, c-1:c+2] == B[r, c])

        # Count the total number of cells in the nine cell window that are stream cells
        n = np.count_nonzero(B[r-1:r+2, c-1:c+2])

        # If the there are only two stream cells in the current block of nine, the block is at the start or end of that stream. Record it as a fixed position. The remaining special
        # cases handle logic associated with the boundary of the stream raster
        if n <= 2 or r == 1 or c == 1 or r == nrows - 2 or c == ncols - 2:
            SEED_r.append(r - 1)
            SEED_c.append(c - 1)
            if B[r, c] > 0:
                i_val = COMID_to_ID[(B[r, c] - MinCOMID)]
                SEED_MinElev.append(E_Min[i_val])
                SEED_MaxElev.append(E_Max[i_val])
                SEEDCONNECT[r - 1, c - 1] = SEED_Val

        # Handle complex connectivity by filling with flag value for later cleaning
        if n != n_same or n > 3:
            SEEDCONNECT[r - 1, c - 1] = CON_Val

    ### Return to the calling function ###
    return SEED_r, SEED_c, SEEDCONNECT


def Clean_Connections_NewMethod(SEEDCONNECT, B, nrows, ncols, ConnectVal, sd):
    #Clean-up the Connections
    CON_r = []
    CON_c = []
    
    #SEEDCONNECT is nrows, ncols
    #B is nrows+2, ncols+2
    
    #LOG.info(SEEDCONNECT.shape)
    #LOG.info(B.shape)
    
    # (CON_RR,CON_CC) = SEEDCONNECT.nonzero()
    (CON_RR,CON_CC) = np.where(SEEDCONNECT > 0)
    num_nonzero = len(CON_RR)
    num_cleaned = 0
    for i in range(num_nonzero):
        r = CON_RR[i]
        c = CON_CC[i]
        if SEEDCONNECT[r,c]>0 and r>sd and r<(nrows-sd) and c>sd and c<(ncols-sd):
            n = np.count_nonzero(SEEDCONNECT[r-sd:r+sd+1,c-sd:c+sd+1]==ConnectVal)
            if n>1:
                Current_COMID = B[r+1,c+1]
                SEED_Times_B = B[r+1-sd:r+1+sd+1,c+1-sd:c+1+sd+1]*SEEDCONNECT[r-sd:r+sd+1,c-sd:c+sd+1]
                n_same = np.count_nonzero(SEED_Times_B==Current_COMID*ConnectVal)  #Within the Stream network, this is how many have the same COMID value
                
                if n_same>1:
                    SEED_Times_B = SEED_Times_B.astype(int)
                    (R_Same, C_Same) = np.where(SEED_Times_B==Current_COMID*ConnectVal)
                    
                #Do we have multiple CONNECT cells with the same stream ID in the same area?
                    if n_same>3:
                        #We have a bunch, so let's just find the centroid-ish and use it.
                        r_avg = 0.0 
                        c_avg = 0.0
                        n_avg = 0
                        r1=-999
                        c1=-999
                        for rrr in range(r-sd,r+sd+1):
                            for ccc in range(c-sd,c+sd+1):
                                if SEEDCONNECT[rrr,ccc]==ConnectVal and B[rrr+1,ccc+1]==Current_COMID:
                                    if r1<-99:
                                        r1=rrr
                                        c1=ccc
                                    n_avg = n_avg + 1
                                    r_avg = r_avg + rrr
                                    c_avg = c_avg + ccc
                                    num_cleaned = num_cleaned + 1
                                    SEEDCONNECT[rrr,ccc] = 0 
                        #Now set the cell towards the center of the mass of cells
                        r_use = int(round(r_avg/n_avg,0))
                        c_use = int(round(c_avg/n_avg,0))
                        #Do this if the most center cell doesn't actually have the value we're after.
                        if B[r_use+1,c_use+1]!=Current_COMID:
                            r_use = r1
                            c_use = c1
                    else:
                        LOG.info(SEED_Times_B)
                        
                        LOG.info(R_Same)
                        LOG.info(C_Same)
                        
                        n_connections_max = 0
                        for xyz in range(n_same):
                            r_temp = R_Same[xyz]
                            c_temp = C_Same[xyz]
                            n_connections_local = np.count_nonzero(B[r+1-sd:r+1+sd+1,c+1-sd:c+1+sd+1]*SEEDCONNECT[r-sd:r+sd+1,c-sd:c+sd+1])
                        
                    CON_r.append(r_use)
                    CON_c.append(c_use)
    '''
                r_avg = 0.0 
                c_avg = 0.0
                n_avg = 0
                r1=-999
                c1=-999
                for rrr in range(r-sd,r+sd+1):
                    for ccc in range(c-sd,c+sd+1):
                        if SEEDCONNECT[rrr,ccc]==ConnectVal:
                            if r1<-99:
                                r1=rrr
                                c1=ccc
                            n_avg = n_avg + 1
                            r_avg = r_avg + rrr
                            c_avg = c_avg + ccc
                            num_cleaned = num_cleaned + 1
                            SEEDCONNECT[rrr,ccc] = 0 
                #Now set the cell towards the center of the mass of cells
                r_use = int(round(r_avg/n_avg,0))
                c_use = int(round(c_avg/n_avg,0))
                if B[r_use+1,c_use+1]==0:
                    LOG.info('Adjusting due to non-value')
                    r_use = r1
                    c_use = c1
                CON_r.append(r_use)
                CON_c.append(c_use)
    #Refill Raster with Connected Values
    num_connect = len(CON_r)
    for i in range(num_connect): 
        r = CON_r[i]
        c = CON_c[i]
        SEEDCONNECT[r,c] = ConnectVal
    '''
    LOG.info('SEEDCONNECT Cleaned ' + str(num_cleaned))
    return SEEDCONNECT, CON_r, CON_c


def Clean_Connections(SEEDCONNECT, B, nrows, ncols, ConnectVal, sd):
    # todo: doc string

    #Clean-up the Connections
    CON_r = []
    CON_c = []

    ### Get data on the locations to be cleaned ###
    # Get the row, column index
    CON_RR, CON_CC = SEEDCONNECT.nonzero()

    # Get the number of cells
    num_nonzero = len(CON_RR)

    # Create a counter for the cells adjusted
    num_cleaned = 0

    ### Process the connections
    if sd <= 0:
        # Don't need to clean any
        LOG.info('Not Cleaning any SEEDCONNECT because sd<=0')
        return SEEDCONNECT, CON_RR, CON_CC
    
    
    for i in range(num_nonzero):
        r = CON_RR[i]
        c = CON_CC[i]
        if SEEDCONNECT[r,c]>0 and r>sd and r<(nrows-sd) and c>sd and c<(ncols-sd):
            n = np.count_nonzero(SEEDCONNECT[r-sd:r+sd+1,c-sd:c+sd+1]==ConnectVal)
            if n>1:
                r_avg = 0.0 
                c_avg = 0.0
                n_avg = 0
                r1=-999
                c1=-999
                for rrr in range(r-sd,r+sd+1):
                    for ccc in range(c-sd,c+sd+1):
                        if SEEDCONNECT[rrr,ccc]==ConnectVal:
                            if r1<-99:
                                r1=rrr
                                c1=ccc
                            n_avg = n_avg + 1
                            r_avg = r_avg + rrr
                            c_avg = c_avg + ccc
                            num_cleaned = num_cleaned + 1
                            SEEDCONNECT[rrr,ccc] = 0 
                #Now set the cell towards the center of the mass of cells
                r_use = int(round(r_avg/n_avg,0))
                c_use = int(round(c_avg/n_avg,0))
                if B[r_use+1,c_use+1]==0:
                    LOG.info('Adjusting due to non-value')
                    r_use = r1
                    c_use = c1
                CON_r.append(r_use)
                CON_c.append(c_use)
    #Refill Raster with Connected Values
    num_connect = len(CON_r)
    for i in range(num_connect): 
        r = CON_r[i]
        c = CON_c[i]
        SEEDCONNECT[r,c] = ConnectVal
    LOG.info('SEEDCONNECT Cleaned ' + str(num_cleaned))
    return SEEDCONNECT, CON_r, CON_c

def Assign_Connections(nrows, ncols, CON_r, CON_c, B, SEEDCONNECT, COMID_Unique, COMID_to_ID, MinCOMID, num_comids, E_Avg, CON_Val, sd):
    # todo: doc string

    ### Create the data structures ###
    Upstream_Connection = [0] * num_comids
    Downstream_Connection = [0] * num_comids

    for i in range(num_comids):
        Upstream_Connection[i] = []

    ### Loop and calculate the connectivity ###
    # Get the number of unique cells
    num_nonzero = len(CON_r)

    # Loop over the unique cells
    for i in range(num_nonzero):
        # Get the row and column index of the cell
        r = CON_r[i] + 1  # Have to have the plus one because the B raster is one cell larger on all sides
        c = CON_c[i] + 1

        # If the cell is within the buffer space form the edge of the raster, attempt to connect it.
        if r > sd and r < nrows + 2 - sd and c > sd and c < ncols + 2 - sd:
            # Get the stream cells within a buffer distances surrounding the target cell
            C_List = B[r - sd:r + sd + 1, c - sd:c + sd + 1].flatten()

            # Get the unique values from the set
            C_List = np.unique(C_List)

            # # If zero exists in the set, delete it since it's not used
            # C_List = np.delete(C_List, 0)

            # # If -9999 exists in the set, delete it since it's not used
            # C_List = np.delete(C_List, -9999)

            C_List = C_List[np.where(C_List > 0)]

            # The number of remaining values are the number of connections in the buffer region
            num_connections = len(C_List)

            # If there is more than one value, attempt to connect it.
            if num_connections > 1:
                # Create a placeholder list for the connection information
                I_List = [0] * num_connections  # This is the ID value associated with each of the values in the C_List
                E_List = [0] * num_connections  # This is the Elevation associated with each of the values in the C_List

                Min_I = -1
                Min_C = -1
                MinElev = 999999999999

                # Loop over each connection
                for i in range(num_connections):
                    # Extract the id and elevation for each connection
                    I_List[i] = COMID_to_ID[C_List[i] - MinCOMID]
                    E_List[i] = int(E_Avg[I_List[i]] * 1000)

                    # Swap values to keep the minimum elevation connection
                    if E_List[i] < MinElev:
                        MinElev = E_List[i]
                        Min_I = I_List[i]
                        Min_C = C_List[i]

                # Set the Upstream Connections
                for i in range(num_connections):
                    if E_List[i] != MinElev:
                        Upstream_Connection[Min_I].append(C_List[i])
                        Downstream_Connection[I_List[i]] = Min_C

    ### Return to the calling function ###
    return Upstream_Connection, Downstream_Connection

@njit(cache=True)
def FindNext(r, c,  ncols, RC_Set: set, B, ANCHOR, dx, dy, dz):
    #Trying to find a connection, but prefer at first prefer it is NOT and Acnhor and that it has the SAME COMID value
    COMID_Current = B[r,c]
    anchor_candidate = (-9999, -9999, 0.0)
    offsets = (
        (1, 0),
        (-1, 0),
        (0, 1),
        (0, -1),
        (1, 1),
        (1, -1),
        (-1, -1),
        (-1, 1),
    )

    seen = False
    for i in range(8):
        rrr = offsets[i][0]
        ccc = offsets[i][1]
        rtest = r+rrr
        ctest = c+ccc
        rc = rtest*(ncols+2)+ctest
        if rrr == 0:
            d = dx
        elif ccc == 0:
            d = dy
        else:
            d = dz

        if rc not in RC_Set and (B[rtest,ctest]==COMID_Current) and ANCHOR[rtest,ctest]<=0:
            return rtest, ctest, d
        
        #Can't find a connection to a similar stream type (COMID), therefore search for an ANCHOR
        if not seen and rc not in RC_Set and ANCHOR[rtest,ctest]>0:
            anchor_candidate = (rtest, ctest, d)
            seen = True
    
    return anchor_candidate

@njit(cache=True)
def FindPathToNextAnchor(r_start, c_start, ncols, ANCHOR, B, E, dx, dy, dz, num_limit):
    R_List=[] 
    C_List=[]
    D_List=[]
    RC_Set = set()

    E_Start = E[r_start, c_start]
    E_End = -9999.9
    r=r_start
    c=c_start

    R_List.append(r_start-1)
    C_List.append(c_start-1)
    RC_Set.add(r_start*(ncols+2)+c_start)
    
    E_Min = 99999999.9
    
    n=0
    while r>0:
        n=n+1 
        if n>num_limit:
            return R_List, C_List,  E_Start, E_End, E_Min, D_List
        next_ = FindNext(r, c, ncols, RC_Set, B, ANCHOR, dx, dy, dz)
        r_next = next_[0]
        c_next = next_[1]
        d = next_[2]

        r=r_next
        c=c_next
        if r_next>0:
            R_List.append(r_next-1)
            C_List.append(c_next-1)
            RC_Set.add(r_next*(ncols+2)+c_next)
            D_List.append(d)
            if E[r_next][c_next] < E_Min:
                E_Min = E[r_next][c_next]
        if r_next>0 and ANCHOR[r,c]>0:
            E_End = E[r,c]
            r=-9999

        #Couldn't find a connecting stream, so we're done here.
        if r_next<0:
            E_End = E[r,c]
            r=-9999
    
    
    #In reality, we want E_Start to be the higher elevation
    if E_Start > E_End:
        return R_List, C_List, E_Start, E_End, E_Min, D_List

    R_List.reverse()
    C_List.reverse()
    D_List.reverse()
    
    return R_List, C_List, E_End, E_Start, E_Min, D_List


def Adjust_Start_End_Elevations_By_Perpendicular_Cells(E_Start, E_End, R_List, C_List, nrows, ncols, B, DEM, searchdist):
    num_pnts = len(R_List)
    
    #Evaluate E_Start first
    #r = R_List[0]
    #c= C_List[0]
    #E_Start = DEM[r,c]
    E_S = E_Start
    
    #r = R_List[num_pnts-1]
    #c= C_List[num_pnts-1]
    #E_End = DEM[r,c]
    E_E = E_End
    
    sd = searchdist
    if sd>num_pnts:
        sd = num_pnts
    if sd>0:
        for s in range(1,sd):
            #STat
            dr = R_List[s] - R_List[0]
            dc = C_List[s] - C_List[0]
            
            #To get perpendicular, swithc the r and c values and make one negative of what it was
            test_r = int(R_List[0] + dc)
            test_c = int(C_List[0] + int(-1*dr))
            #LOG.info(str(dr) + ' ' + str(dc) + '  ' + str(E_Start)  + '  Perpendicular Cells:  ' + str(test_r) + '  ' + str(test_c) + '  ' + str(DEM[test_r,test_c]))
            if test_r>=0 and test_r<nrows and test_c>=0 and test_c<ncols and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<E_Start:
                E_Start = DEM[test_r,test_c]
            
            test_r = int(R_List[0] + int(-1*dc))
            test_c = int(R_List[0] + dr)
            #LOG.info(str(dr) + ' ' + str(dc) + '  ' + str(E_Start)  + '  Perpendicular Cells:  ' + str(test_r) + '  ' + str(test_c) + '  ' + str(DEM[test_r,test_c]))
            if test_r>=0 and test_r<(nrows-1) and test_c>=0 and test_c<(ncols-1) and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<E_Start:
                E_Start = DEM[test_r,test_c]
            
            #Now look at E_End
            dr = R_List[num_pnts-1-s] - R_List[num_pnts-1]
            dc = C_List[num_pnts-1-s] - C_List[num_pnts-1]
            
            #To get perpendicular, swithc the r and c values and make one negative of what it was
            test_r = int(R_List[num_pnts-1] + int(dc))
            test_c = int(C_List[num_pnts-1] + int(-1*dr))
            if test_r>=0 and test_r<(nrows-1) and test_c>=0 and test_c<(ncols-1) and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<E_End:
                E_End = DEM[test_r,test_c]
            
            test_r = int(C_List[num_pnts-1] + int(-1*dc))
            test_c = int(C_List[num_pnts-1] + int(dr))
            if test_r>=0 and test_r<nrows and test_c>=0 and test_c<ncols and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<E_End:
                E_End = DEM[test_r,test_c]
    if abs(E_Start-E_S)>0.001:
        LOG.info(str(B[R_List[0]+1,C_List[0]+1]) + '  Changed Starting Elevation from ' + str(E_S) + ' to ' + str(E_Start))
    if abs(E_End-E_E)>0.001:
        LOG.info(str(B[R_List[-1]+1,C_List[-1]+1]) + '  Changed Ending Elevation from ' + str(E_E) + ' to ' + str(E_End))
    
    #if int(B[R_List[0]+1,C_List[0]+1])==1841439:
    #    LOG.info('\n\n')
    #    LOG.info(str(B[R_List[0]+1,C_List[0]+1]) + '  Changed Starting Elevation from ' + str(E_S) + ' to ' + str(E_Start))
    #    LOG.info(str(B[R_List[-1]+1,C_List[-1]+1]) + '  Changed Ending Elevation from ' + str(E_E) + ' to ' + str(E_End))
    
    return E_Start, E_End


def Adjust_Elevations_By_Perpendicular_Cells(E_Original, i, R_List, C_List, nrows, ncols, B, DEM, searchdist, Flood):
    num_pnts = len(R_List)
    
    E_Updated = E_Original
    
    if i<=(num_pnts*0.5):
        sd = num_pnts-i
        if sd>searchdist:
            sd = searchdist
        if sd>0:
            for s in range(i+1,i+sd):
                #STat
                dr = R_List[s] - R_List[i]
                dc = C_List[s] - C_List[i]
                
                #To get perpendicular, switch the r and c values and make one negative of what it was
                test_r = int(R_List[i] + dc)
                test_c = int(C_List[i] + int(-1*dr))
                #LOG.info(str(dr) + ' ' + str(dc) + '  ' + str(E_Start)  + '  Perpendicular Cells:  ' + str(test_r) + '  ' + str(test_c) + '  ' + str(DEM[test_r,test_c]))
                if test_r>=0 and test_r<nrows and test_c>=0 and test_c<ncols and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<E_Updated and Flood[test_r,test_c]>0:
                    E_Updated = DEM[test_r,test_c]
                
                test_r = int(R_List[0] + int(-1*dc))
                test_c = int(C_List[0] + dr)
                #LOG.info(str(dr) + ' ' + str(dc) + '  ' + str(E_Start)  + '  Perpendicular Cells:  ' + str(test_r) + '  ' + str(test_c) + '  ' + str(DEM[test_r,test_c]))
                if test_r>=0 and test_r<(nrows-1) and test_c>=0 and test_c<(ncols-1) and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<E_Updated and Flood[test_r,test_c]>0:
                    E_Updated = DEM[test_r,test_c]
                
                #if int(B[R_List[i]+1,C_List[i]+1])==1841439:
                #    LOG.info('   ' + str(dr) + ' ' + str(dc) + '  ' + str(E_Original)  + '  Perpendicular Cells:  ' + str(test_r) + '  ' + str(test_c) + '  ' + str(E_Updated))
    else:
        sd = i
        if sd>searchdist:
            sd = i-searchdist
        if sd>0:
            for s in range(i-1,sd,-1):
                #STat
                dr = R_List[s] - R_List[i]
                dc = C_List[s] - C_List[i]
                
                #To get perpendicular, switch the r and c values and make one negative of what it was
                test_r = int(R_List[i] + dc)
                test_c = int(C_List[i] + int(-1*dr))
                #print(str(dr) + ' ' + str(dc) + '  ' + str(E_Start)  + '  Perpendicular Cells:  ' + str(test_r) + '  ' + str(test_c) + '  ' + str(DEM[test_r,test_c]))
                if test_r>=0 and test_r<nrows and test_c>=0 and test_c<ncols and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<E_Updated and Flood[test_r,test_c]>0:
                    E_Updated = DEM[test_r,test_c]
                
                test_r = int(R_List[0] + int(-1*dc))
                test_c = int(C_List[0] + dr)
                #print(str(dr) + ' ' + str(dc) + '  ' + str(E_Start)  + '  Perpendicular Cells:  ' + str(test_r) + '  ' + str(test_c) + '  ' + str(DEM[test_r,test_c]))
                if test_r>=0 and test_r<(nrows-1) and test_c>=0 and test_c<(ncols-1) and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<E_Updated and Flood[test_r,test_c]>0:
                    E_Updated = DEM[test_r,test_c]
                
    #if abs(E_Updated-E_Original)>0.001:
    #    LOG.info(str(B[R_List[i]+1,C_List[i]+1]) + '  Changed Elevation from ' + str(E_Original) + ' to ' + str(E_Updated))
    
    #if int(B[R_List[i]+1,C_List[i]+1])==1841439:
    #    asdfasdfasdf
    return E_Updated


def Plot_Elevation_Profiles(COMID_Name, Dist, DEM, ModDEM):
    plt.figure()
    plt.plot(Dist,DEM,'k')
    plt.plot(Dist,ModDEM,'r')
    plt.title(COMID_Name)
    plt.savefig((str(COMID_Name)+'.png'))
    plt.close()
    return

def FloodAllLocalAreas(WSE, E_Box, r_min, r_max, c_min, c_max, r_use, c_use):
    FourMatrix = np.zeros((3,3)) + 4
    
    nrows_local = r_max-r_min+2
    ncols_local = c_max-c_min+2
    FloodLocal = np.zeros((nrows_local,ncols_local))
    
    FloodLocal[1:nrows_local-1,1:ncols_local-1] = np.where(E_Box<=WSE,1,0)
    
    #This is the Stream Cell.  Mark it with a 4
    FloodLocal[(r_use-r_min+1),(c_use-c_min+1)] = 4 
    
    #Go through and mark all the cells that 
    for r in range((r_use-r_min+1),nrows_local-1):
        for c in range((c_use-c_min+1),ncols_local-1):
            #LOG.info(FloodLocal[r-1:r+2,c-1:c+2].shape)
            #LOG.info(FourMatrix.shape)
            #LOG.info(FloodLocal[r-1:r+2,c-1:c+2])
            if FloodLocal[r,c]>=3:
                FloodLocal[r-1:r+2,c-1:c+2] = FloodLocal[r-1:r+2,c-1:c+2] * FourMatrix
    for r in range((r_use-r_min+1), 0, -1):
        for c in range((c_use-c_min+1), 0, -1):
            if FloodLocal[r,c]>=3:
                FloodLocal[r-1:r+2,c-1:c+2] = FloodLocal[r-1:r+2,c-1:c+2] * FourMatrix
    
    for r in range(1, nrows_local-1):
        for c in range(1, ncols_local-1):
            if FloodLocal[r,c]>=3:
                FloodLocal[r-1:r+2,c-1:c+2] = FloodLocal[r-1:r+2,c-1:c+2] * FourMatrix
    
    #LOG.info(FloodLocal)
    #FloodReturn = np.where(FloodLocal[1:nrows_local-1,1:ncols_local-1]>0.0,1.0,0.0)
    #LOG.info(np.where(FloodLocal[1:nrows_local-1,1:ncols_local-1]>3.0,1.0,0.0))
    return np.where(FloodLocal[1:nrows_local-1,1:ncols_local-1]>3.0,1.0,0.0)


def CreateWeightAndElipseMask(TW, dx, dy):
    ElipseMask = np.zeros((TW+1,int(TW*2+1),int(TW*2+1)), dtype=np.bool_)  #3D Array
    WeightBox = np.zeros((int(TW*2+1),int(TW*2+1)))  #2D Array
    for i in range(1,TW+1):
        TWDX = i*dx*i*dx
        TWDY = i*dy*i*dy
        for r in range(0,i+1):
            for c in range(0,i+1):
                is_elipse = (c*dx*c*dx/(TWDX)) + (r*dy*r*dy/(TWDY))   #https://www.mathopenref.com/coordgeneralellipse.html
                if is_elipse<=1.0:
                    ElipseMask[i,TW+r,TW+c] = True
                    ElipseMask[i,TW-r,TW+c] = True
                    ElipseMask[i,TW+r,TW-c] = True
                    ElipseMask[i,TW-r,TW-c] = True
    #LOG.info(ElipseMask[2,TW-4:TW+4+1,TW-4:TW+4+1].astype(int))
    #LOG.info(ElipseMask[10,TW-14:TW+14+1,TW-14:TW+14+1].astype(int))
    #LOG.info(ElipseMask[40,TW-44:TW+44+1,TW-44:TW+44+1].astype(int))
    
    for r in range(0,TW+1):
        for c in range(0,TW+1):
            z = pow((c*dx*c*dx + r*dy*r*dy), 0.5)
            if z<0.0001:
                z=0.001
            WeightBox[TW+r,TW+c] = 1 / (z*z)
            WeightBox[TW-r,TW+c] = 1 / (z*z)
            WeightBox[TW+r,TW-c] = 1 / (z*z)
            WeightBox[TW-r,TW-c] = 1 / (z*z)
    
    return WeightBox, ElipseMask

def CreateSimpleFloodMap(RR, CC, E, B, nrows, ncols, y_depth, sd, TW_m, dx, dy, LocalFloodOption, COMID_Unique, COMID_to_ID, MinCOMID, COMID_Unique_TW, COMID_Unique_Depth, WeightBox, ElipseMask, TW):
    WSE_Times_Weight = np.zeros((nrows+2,ncols+2), dtype=float)
    Total_Weight = np.zeros((nrows+2,ncols+2), dtype=float)
    
    #Now go through each cell
    num_nonzero = len(RR)
    for i in range(num_nonzero):
        r = RR[i]
        c = CC[i]
        r_use = r
        c_use = c
        E_Min = E[r,c]
        
        #Get COMID, TopWidth, and Depth Information for this cell
        COMID_Value = int(B[r,c])
        iii = COMID_to_ID[COMID_Value - MinCOMID]
        COMID_TW_m = COMID_Unique_TW[iii]
        COMID_D = COMID_Unique_Depth[iii]
        if COMID_TW_m > TW_m:
            COMID_TW_m = TW_m
        COMID_TW = int(max(round(COMID_TW_m/dx,0),round(COMID_TW_m/dy,0)))  #This is how many cells we will be looking at surrounding our stream cell
        if COMID_TW<=1:
            COMID_TW=2
        
        #Find minimum elevation within the search box
        if sd<1:
            r_use = r
            c_use = c
        else:
            for rr in range(r-sd,r+sd+1):
                for cc in range(c-sd,c+sd+1):
                    if rr>0 and rr<(nrows-1) and cc>0 and cc<=(ncols-1) and E[rr,cc]>0.1 and E[rr,cc] < E_Min:
                        E_Min = E[rr,cc]
                        r_use = rr
                        c_use = cc
        
        #Now start with rows and start flooding everything in site
        #WSE = float(E[r_use,c_use] + y_depth)
        WSE = float(E[r_use,c_use] + COMID_D)
        r_min = r_use-COMID_TW
        r_max = r_use+COMID_TW+1
        if r_min<1:
            r_min = 1 
        if r_max>(nrows+1):
            r_max=nrows+1
        c_min = c_use-COMID_TW
        c_max = c_use+COMID_TW+1
        if c_min<1:
            c_min = 1 
        if c_max>(ncols+1):
            c_max=ncols+1
        
        #Find what would flood local
        if LocalFloodOption:
            E_Box = E[r_min:r_max,c_min:c_max]
            FloodLocalMask = FloodAllLocalAreas(WSE, E_Box, r_min, r_max, c_min, c_max, r_use, c_use)
        
        #This uses the weighting method from FloodSpreader to create a flood map
        #   Here we use TW instead of COMID_TW.  This is because we are trying to find the center of the weight raster, which was set based on TW (not COMID_TW).  COMID_TW mainly applies to the r_min, r_max, c_min, c_max
        w_r_min = TW-(r_use-r_min)
        w_r_max = TW+r_max-r_use
        w_c_min = TW-(c_use-c_min)
        w_c_max = TW+c_max-c_use
        
        #These all should have the same shape
        #LOG.info(WSE_Times_Weight[r_min:r_max,c_min:c_max].shape)
        #LOG.info(WeightBox[w_r_min:w_r_max,w_c_min:w_c_max].shape)
        #LOG.info(FloodLocalMask.shape)
        
        #Create the Weight Matrix.  Account for the Weight Box as well as what would actually flood Locally
        if LocalFloodOption:
            WSE_Times_Weight[r_min:r_max,c_min:c_max] = WSE_Times_Weight[r_min:r_max,c_min:c_max] + WSE * WeightBox[w_r_min:w_r_max,w_c_min:w_c_max] * ElipseMask[COMID_TW, w_r_min:w_r_max,w_c_min:w_c_max] * FloodLocalMask
            Total_Weight[r_min:r_max,c_min:c_max] = Total_Weight[r_min:r_max,c_min:c_max] + WeightBox[w_r_min:w_r_max,w_c_min:w_c_max] * ElipseMask[COMID_TW, w_r_min:w_r_max,w_c_min:w_c_max] * FloodLocalMask
        else:
            WSE_Times_Weight[r_min:r_max,c_min:c_max] = WSE_Times_Weight[r_min:r_max,c_min:c_max] + WSE * WeightBox[w_r_min:w_r_max,w_c_min:w_c_max] * ElipseMask[COMID_TW, w_r_min:w_r_max,w_c_min:w_c_max]
            Total_Weight[r_min:r_max,c_min:c_max] = Total_Weight[r_min:r_max,c_min:c_max] + WeightBox[w_r_min:w_r_max,w_c_min:w_c_max] * ElipseMask[COMID_TW, w_r_min:w_r_max,w_c_min:w_c_max]
        
    WSE_divided_by_weight = WSE_Times_Weight / Total_Weight
    Flooded = np.where(WSE_divided_by_weight>E,1,0).astype(np.uint8)
    
    #Also make sure all the Cells that have Stream are counted as flooded.
    for i in range(num_nonzero):
        Flooded[RR[i],CC[i]] = 1

    return Flooded

@njit(cache=True)
def CreateFloodImpactMap(Flood, B, nrows, ncols, TW_m, dx, dy, COMID_to_ID, MinCOMID, COMID_Unique_TW, ElipseMask, TW):
    FloodImpact = np.zeros((nrows+2,ncols+2), dtype=np.int32)
    
    #Find all the flooded cells
    # (Flood_R, Flood_C)= Flood.nonzero()
    (Flood_R, Flood_C)= np.where(Flood > 0)

    num_flooded_cells = len(Flood_R)
        
    p_increment=int(num_flooded_cells/20)
    pcount = p_increment
    for i in range(num_flooded_cells):
        if i>=pcount:
            pcount = pcount + p_increment
        
        r = Flood_R[i]
        c = Flood_C[i]
        r_use = r
        c_use = c
        
        #Find the maximum possible search radius we need to evaluate.
        #This is for the raster
        r_min = r_use-TW
        r_max = r_use+TW+1
        if r_min<1:
            r_min = 1 
        if r_max>(nrows+1):
            r_max=nrows+1
        c_min = c_use-TW
        c_max = c_use+TW+1
        if c_min<1:
            c_min = 1 
        if c_max>(ncols+1):
            c_max=ncols+1
        
        #This is for the elipse mask.
        #Here we use TW instead of COMID_TW.
        w_r_min = TW-(r_use-r_min)
        w_r_max = TW+r_max-r_use
        w_c_min = TW-(c_use-c_min)
        w_c_max = TW+c_max-c_use
        
        Flood_Raster_Potential_Influence = B[r_min:r_max,c_min:c_max] * ElipseMask[TW, w_r_min:w_r_max,w_c_min:w_c_max]
        
        #Get list of Unique Values
        Subset_Unique = np.unique(Flood_Raster_Potential_Influence)
        Subset_Unique = Subset_Unique[Subset_Unique > 0]

        COMID_to_Print = 0
        if len(Subset_Unique)<1:
            COMID_to_Print = 0
        elif len(Subset_Unique)==1:
            COMID_to_Print = Subset_Unique[0]
        else:
            COMID_Instance_Count = 0
            for COMID_V in Subset_Unique:
                COMID_Value = int(COMID_V)
                iii = COMID_to_ID[COMID_Value - MinCOMID]
                COMID_TW_m = COMID_Unique_TW[iii]
                if COMID_TW_m > TW_m:
                    COMID_TW_m = TW_m
                COMID_TW = int(max(round(COMID_TW_m/dx,0),round(COMID_TW_m/dy,0)))  #This is how many cells we will be looking at surrounding our stream cell
                if COMID_TW<=1:
                    COMID_TW=2
                
                #Now we want to go smaller based on the TW of the specific COMID.  Do we still see impact from those.
                #Note that the these now use COMID_TW, not TW

                Subset_Flood_Raster_Potential_Influence = B[r_min:r_max,c_min:c_max] * ElipseMask[COMID_TW, w_r_min:w_r_max,w_c_min:w_c_max]
                
                count_for_comid = np.count_nonzero(Subset_Flood_Raster_Potential_Influence == COMID_Value)
                if count_for_comid > COMID_Instance_Count:
                    COMID_to_Print = COMID_Value
                    COMID_Instance_Count = count_for_comid
                
            
        #Find R and C for each one to see if they are even within distance to influence this flooded cell.
        
        #Find cell that has impact the most times and record it in the 'FloodImpact' Raster.
        FloodImpact[r,c] = COMID_to_Print
    
    return FloodImpact


def MergeStreamElevationsWithDEM(E, B, Flood, FloodImpact, Elev_Streams, ES_R, ES_C, ncols, nrows, TW_m, dx, dy, COMID_Unique, COMID_to_ID, MinCOMID, COMID_Unique_TW, COMID_Unique_Depth, WeightBox, ElipseMask, TW):
    Elev_Times_Weight = np.zeros((nrows+2,ncols+2), dtype=np.float64)
    Total_Weight = np.zeros((nrows+2,ncols+2), dtype=np.float64)
    
    num_nonzero = len(ES_R)
    for i in range(num_nonzero):
        r_use = ES_R[i] + 1  #Need the +1 because ES_R and ES_C are based on the standard raster, not the raster with 1-cell added on each side.
        c_use = ES_C[i] + 1
        
        #Get COMID, TopWidth, and Depth Information for this cell
        COMID_Value = B[r_use,c_use]
        if COMID_Value <= 0:
            continue

        iii = COMID_to_ID[COMID_Value - MinCOMID]
        COMID_TW_m = COMID_Unique_TW[iii]
        if COMID_TW_m > TW_m:
            COMID_TW_m = TW_m
        COMID_TW = int(max(round(COMID_TW_m/dx,0),round(COMID_TW_m/dy,0)))  #This is how many cells we will be looking at surrounding our stream cell
        if COMID_TW<=1:
            COMID_TW=2
        
        #Now start with rows and start evaluating all surrounding cells
        ELEV = float(Elev_Streams[ES_R[i],ES_C[i]])
        r_min = r_use-COMID_TW
        r_max = r_use+COMID_TW+1
        if r_min<1:
            r_min = 1 
        if r_max>(nrows+1):
            r_max=nrows+1
        c_min = c_use-COMID_TW
        c_max = c_use+COMID_TW+1
        if c_min<1:
            c_min = 1 
        if c_max>(ncols+1):
            c_max=ncols+1
        
        #This uses the weighting method from FloodSpreader to create a flood map
        #   Here we use TW instead of COMID_TW.  This is because we are trying to find the center of the weight raster, which was set based on TW (not COMID_TW).  COMID_TW mainly applies to the r_min, r_max, c_min, c_max
        w_r_min = TW-(r_use-r_min)
        w_r_max = TW+r_max-r_use
        w_c_min = TW-(c_use-c_min)
        w_c_max = TW+c_max-c_use
        
        
        #Create an Impact multiplier that is 1 if the cell is flooded by the current COMID, and 0 if it is not
        #FloodBigImpact[r_min:r_max,c_min:c_max].fill(0)  #Sets all the values to 0
        # FloodBigImpact[r_min:r_max,c_min:c_max] = np.where(FloodImpact[r_min:r_max,c_min:c_max]==COMID_Value, 1.0, 0.0)
        
        
        #The FloodBig basically clips any analysis to only the cells that are considered flooded.
        #  OIn 2/7/2024 I started using 'FloodBigImpact' instead of 'FloodBig'
        mask = WeightBox[w_r_min:w_r_max,w_c_min:w_c_max] * np.where(FloodImpact[r_min:r_max,c_min:c_max]==COMID_Value, 1.0, 0.0) * ElipseMask[COMID_TW, w_r_min:w_r_max,w_c_min:w_c_max]
        Elev_Times_Weight[r_min:r_max,c_min:c_max] += ELEV * mask
        Total_Weight[r_min:r_max,c_min:c_max] += mask
        
        #LOG.info(ELEV)
        #LOG.info(Elev_Times_Weight[r_min:r_max,c_min:c_max])
        #LOG.info(FloodBig[r_min:r_max,c_min:c_max])
        #LOG.info(Elev_Times_Weight[r_min:r_max,c_min:c_max].max())
        #LOG.info(FloodBig[r_min:r_max,c_min:c_max].max())
    
    Total_Weight[Total_Weight == 0] = 1e-12 
    Elev_divided_by_weight = Elev_Times_Weight / Total_Weight
    
    #If a cell is in the channel (determined by Flood raster) then use the weighted stream elevation, otherwise use the DEM data (E)
    ModifiedDEM = np.where(Elev_divided_by_weight>0.0,Elev_divided_by_weight,E)
    return ModifiedDEM

def Last_Ditch_Effort_To_Smooth_Stream_Bumps(Elev_Streams, nrows, ncols, CON_r, CON_c):
    num_to_evaluate = len(CON_r)
    
    for x in range(num_to_evaluate):
        #e = Elev_Streams[CON_r[x],CON_c[x]]
        for r in range(CON_r[x]-1, CON_r[x]+2):
            for c in range(CON_c[x]-1, CON_c[x]+2):
                if r>=0 and r<nrows and c>=0 and c<ncols and Elev_Streams[r,c]>0.0:
                    if Elev_Streams[r,c] < Elev_Streams[CON_r[x],CON_c[x]]:
                        Elev_Streams[CON_r[x],CON_c[x]] = Elev_Streams[r,c]
    return

def DEM_Cleaner_Program(OutputID, 
                        StreamShapefile, 
                        DEM_Folder, 
                        DEM_List, 
                        STRM_File_List, 
                        Working_Folder, 
                        FlowFileName, 
                        CurveParamFileName, 
                        FloodMapName, 
                        Q_Fraction, 
                        TopWidthPlausibleLimit, 
                        search_dist_for_min_elev, 
                        search_dist_perp_cells):
    
    StrmBase = GetStreamlineBaseName(StreamShapefile)
    
    
    for ddd in range(len(DEM_List)):
        LOG.info('Working on Site ' + DEM_List[ddd])
        
        DEM_File = os.path.join(DEM_Folder, DEM_List[ddd])
        STRM_File_Clean = STRM_File_List[ddd]
        if STRM_File_Clean=='':
            STRM_File = Working_Folder + 'STRM_' + DEM_List[ddd]
            
            if '.tif' in STRM_File:
                STRM_File_Clean = STRM_File.replace('.tif','_Clean.tif')
            if '.img' in STRM_File:
                STRM_File_Clean = STRM_File.replace('.img','_Clean.img')
        
            if os.path.isfile(STRM_File):
                LOG.info('Stream File Already Exists')
            else:
                LOG.info('Get the Raster Dimensions for ' + DEM_File)
                (minx, miny, maxx, maxy, dx, dy, ncols, nrows, dem_geoTransform, dem_projection) = Get_Raster_Details(DEM_File)
                cellsize_x = abs(float(dx))
                cellsize_y = abs(float(dy))
                lat_base = float(maxy) - 0.5*cellsize_y
                lon_base = float(minx) + 0.5*cellsize_x
        
                LOG.info('Creating ' + STRM_File)
                
                #This should clip the larger DEM Raster to the correct size
                function_str = 'gdal_rasterize -a_nodata -9999 -ot UInt32 -a ' + OutputID + ' -ts ' + str(ncols) + ' ' + str(nrows) + ' -te ' + ' '.join([str(x) for x in [minx, miny, maxx, maxy]]) + ' -l ' + StrmBase + ' ' + StreamShapefile + ' ' + STRM_File
                LOG.info(function_str)
                subprocess.call(function_str, shell=True)
            
            #Clean the Stream File
            if os.path.isfile(STRM_File_Clean):
                LOG.info('Clean Stream File Already Exists: ' + STRM_File_Clean)
            else:
                LOG.info('Creating Clean Stream File: ' + STRM_File_Clean)
                Clean_STRM_Raster(STRM_File, STRM_File_Clean)
        S = 0
        (S, ncols, nrows, cellsize, yll, yur, xll, xur, lat, s_geotransform, s_projection) = Read_Raster_GDAL(STRM_File_Clean)
        
        #Open DEM File
        (DEM, ncols, nrows, cellsize, yll, yur, xll, xur, lat, dem_geotransform, dem_projection) = Read_Raster_GDAL(DEM_File)
        
        
        
        E = np.zeros((nrows+2,ncols+2), dtype=float)  #Create an array that is slightly larger than the STRM Raster Array
        E[1:(nrows+1), 1:(ncols+1)] = DEM
        
        #Get Cellsize Information
        (dx, dy, dm) = convert_cell_size(cellsize, yll, yur)
        dz = pow(dx*dx+dy*dy,0.5)
        
        #Get list of Uniqe Stream IDs.  Also find where all the cell values are.
        B = np.zeros((nrows+2,ncols+2), dtype=np.int32)  #Create an array that is slightly larger than the STRM Raster Array
        B[1:(nrows+1), 1:(ncols+1)] = S
        # (RR,CC) = B.nonzero()
        (RR,CC) = np.where(B > 0)
        
        COMID_Unique = np.unique(B)
        COMID_Unique = COMID_Unique[np.where(COMID_Unique > 0)]
        #Sort from Smallest to highest values
        COMID_Unique = np.sort(COMID_Unique).astype(int)
        num_comids = len(COMID_Unique)
                
        # Compute necessary values
        MinCOMID = COMID_Unique.min()
        MaxCOMID = COMID_Unique.max()

        LOG.info('\nCOMID Ranges from ' + str(MinCOMID) + ' to ' + str(MaxCOMID))
        
        COMID_to_ID = np.zeros(MaxCOMID-MinCOMID+1).astype(int)
        COMID_to_ID = COMID_to_ID - 1
        
        #Get the Unique Identifier Set
        for i in range(num_comids):
            #LOG.info(str(COMID_Unique[i]) + ' ' + str(i))
            COMID_to_ID[int(COMID_Unique[i]-MinCOMID)] = i
        
        
        
        #Get an Average Flow rate associated with each stream reach.
        COMID_Unique_Flow = FindFlowRateForEachCOMID(FlowFileName, COMID_Unique, COMID_to_ID, MinCOMID, MaxCOMID)
        
        
        #Calculate an Average Top Width and Depth for each stream reach.
        #Q_Fraction = 1.0
        #TopWidthPlausibleLimit = 600.0
        #(COMID_Unique_TW, COMID_Unique_Depth, TopWidthMax) = Calculate_TW_D_ForEachCOMID(CurveParamFileName, COMID_Unique_Flow, COMID_Unique, COMID_to_ID, MinCOMID, Q_Fraction)
        (COMID_Unique_TW, COMID_Unique_Depth, TopWidthMax) = Calculate_TW_D_ForEachCOMID_ARC(CurveParamFileName, COMID_Unique_Flow, COMID_Unique, COMID_to_ID, MinCOMID, MaxCOMID, Q_Fraction)
        LOG.info('Maximum Top Width = ' + str(TopWidthMax))
        for x in range(len(COMID_Unique)):
            if COMID_Unique_TW[x]>TopWidthPlausibleLimit:
                LOG.info('Ignoring ' + str(COMID_Unique[x]) + '  ' + str(COMID_Unique_Flow[x])  + '  ' + str(COMID_Unique_Flow[x]*Q_Fraction) + '  ' + str(COMID_Unique_Depth[x]) + '  ' + str(COMID_Unique_TW[x]))             
        
        if TopWidthPlausibleLimit < TopWidthMax:
            TopWidthMax = TopWidthPlausibleLimit
        
        #Create a Weight Box and an Elipse Mask that can be used for all of the cells
        X_cells = round(TopWidthMax/dx,0)
        Y_cells = round(TopWidthMax/dy,0)
        TW = int(max(Y_cells,X_cells))  #This is how many cells we will be looking at surrounding our stream cell
        
        #Create a Weight and an Elipse Mask
        (WeightBox, ElipseMask) = CreateWeightAndElipseMask(TW, dx, dy)  #3D Array with the same row/col dimensions as the WeightBox
        
        #Create a simple Flood Map
        if os.path.isfile(FloodMapName):
            LOG.info('Using Flood Map: ' + FloodMapName)
            Flood = np.zeros((nrows+2,ncols+2))
            (Flood[1:nrows+1,1:ncols+1], ncols, nrows, cellsize, yll, yur, xll, xur, lat, dem_geotransform, dem_projection) = Read_Raster_GDAL(FloodMapName)
        else:
            LOG.info('YOU NEED TO CREATE AN INITIAL FLOOD MAP!!!')
            return
            #Flood = CreateSimpleFloodMap(RR, CC, E, B, nrows, ncols, y_depth, search_dist_for_min_elev, TopWidthMax, dx, dy, LocalFloodOption, COMID_Unique, COMID_to_ID, MinCOMID, COMID_Unique_TW, COMID_Unique_Depth, WeightBox, ElipseMask, TW)
            #Write_Output_Raster(FloodMapName, Flood[1:nrows+1,1:ncols+1], ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Int32)
        
        #If warranted, write a Flood Impact File
        FloodImpact_File = os.path.join(Working_Folder, 'FLOOD_IMPACT_' + DEM_List[ddd])
        if os.path.isfile(FloodImpact_File):
            LOG.info('Using Existing Flood Map: FLOOD_IMPACT_' + DEM_List[ddd])
            FloodImpact = np.zeros((nrows+2,ncols+2), dtype=np.int32)
            (FloodImpact[1:nrows+1,1:ncols+1], ncols, nrows, cellsize, yll, yur, xll, xur, lat, dem_geotransform, dem_projection) = Read_Raster_GDAL(FloodImpact_File)
        else:
            LOG.info('Creating Flood Impact Map: ' + FloodImpact_File)
            COMID_Unique_TW_Reduced = COMID_Unique_TW * 0.75
            FloodImpact = CreateFloodImpactMap(Flood, B, nrows, ncols, TopWidthMax, dx, dy, COMID_to_ID, MinCOMID, COMID_Unique_TW_Reduced, ElipseMask, TW)
            LOG.info('Finished Process for Flood Impact')
            Write_Output_Raster(FloodImpact_File, FloodImpact[1:nrows+1,1:ncols+1], ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Int32)
        
        
        LOG.info('Finding Stream Data: Length and Elevation Data')
        (E_Min, E_Avg, E_Max) = FindStreamCharacteristics(E, B, COMID_Unique, COMID_to_ID, MinCOMID, RR, CC, num_comids)
        
        
        #Find SEED locations as well as where connections between streams occur.
        LOG.info('Finding SEED and Connection Locations')
        SEED_Val = 1 
        CON_Val = 2
        (SEED_r, SEED_c, SEEDCONNECT) = Find_SEED_and_CONNECTIONS(RR, CC, B, E_Min, E_Max, COMID_to_ID, MinCOMID, nrows, ncols, SEED_Val, CON_Val) 
        (SEEDCONNECT, CON_r, CON_c) = Clean_Connections(SEEDCONNECT, B, nrows, ncols, CON_Val, 0)
        #(SEEDCONNECT, CON_r, CON_c) = Clean_Connections(SEEDCONNECT, B, nrows, ncols, CON_Val, 3)
        #(SEEDCONNECT, CON_r, CON_c) = Clean_Connections_NewMethod(SEEDCONNECT, B, nrows, ncols, CON_Val, 3)
        #(CON_r, CON_c) = SEEDCONNECT.nonzero()
        SEED_CONNECT_FILE = os.path.join(Working_Folder, 'SEED_CONNECT_' + DEM_List[ddd])
        if os.path.isfile(SEED_CONNECT_FILE):
            LOG.info('File Already Exists:  ' + SEED_CONNECT_FILE)
        else:
            Write_Output_Raster(SEED_CONNECT_FILE, SEEDCONNECT, ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Int32)
        
        #Find the Connections between the Stream Reaches
        LOG.info('Finding Where Each Stream Connects')
        (Upstream_Connection, Downstream_Connection) = Assign_Connections(nrows, ncols, CON_r, CON_c, B, SEEDCONNECT, COMID_Unique, COMID_to_ID, MinCOMID, num_comids, E_Avg, CON_Val, 2)

        # todo: here is where an intervention needs to happen

        
        #Set the Anchor Points for the Elevation Dataset
        ANCHOR = np.zeros((nrows+2,ncols+2)) 
        A_R = []
        A_C = []
        A_R.extend(SEED_r)
        A_C.extend(SEED_c)
        A_R.extend(CON_r)
        A_C.extend(CON_c)
        num_anchors = len(A_R)
        A_RC = np.zeros(num_anchors)
        A_Elev = np.zeros(num_anchors)
        for i in range(num_anchors):
            r = A_R[i]
            c = A_C[i]
            A_RC[i] = r*(ncols+2)+c
            A_Elev[i] = DEM[r,c]
            ANCHOR[r+1,c+1] = 1
        
        
        
        #COMIDs_To_Evaluate_List = [750073014, 750045778, 750131024, 750130432, 750132208, 750083664]
        #COMIDs_To_Evaluate_List = [760748000, 760743009, 760703075]
        
        #Create Elevation Streams File
        ElevStreamsName = os.path.join(Working_Folder, 'Elev_Streams_' + DEM_List[ddd])
        if os.path.isfile(ElevStreamsName):
            LOG.info('Using Existing Elevation-Streams File: ' + ElevStreamsName)
            (Elev_Streams, ncols, nrows, cellsize, yll, yur, xll, xur, lat, dem_geotransform, dem_projection) = Read_Raster_GDAL(ElevStreamsName)
        else:
            Elev_Streams = np.zeros((nrows,ncols))
            #Starting at each Anchor Point, Find the Path to the next Anchor point
            for i in range(num_anchors):
                Elev_Streams[A_R[i],A_C[i]] = DEM[A_R[i],A_C[i]]
                if A_RC[i]!=0:
                    COMID_to_Evaluate = B[A_R[i]+1,A_C[i]+1]
                    (R_List, C_List, E_Start, E_End, E_Min_from_Path, D_List) = FindPathToNextAnchor(A_R[i]+1, A_C[i]+1, ncols, ANCHOR, B, E, dx, dy, dz, 9999)
                    
                    #SEt the A_RC to zero for the Anchors we just evaluated.  This prevents redundantly evaluating the same stream cells
                    #aaa = np.where(A_RC==A1)
                    #A_RC[aaa]=0
                    #LOG.info(str(B[A_R[i]+1,A_C[i]+1]) + '   E_Start=' + str(E_Start) + '   E_End=' + str(E_End) + '   Dist=' + str(sum(D_List)))
                    #if A2>0:
                    #    aaa = np.where(A_RC==A2)
                    #    A_RC[aaa]=0
                    
                    
                    estart = E_Start
                    eend = E_End
                    #Adjust the Start and Ending Elevations
                    if search_dist_perp_cells>0:
                        E_Start = Adjust_Elevations_By_Perpendicular_Cells(E_Start, 0, R_List, C_List, nrows, ncols, B, DEM, search_dist_perp_cells, Flood)
                        E_End = Adjust_Elevations_By_Perpendicular_Cells(E_End, (len(R_List)-1), R_List, C_List, nrows, ncols, B, DEM, search_dist_perp_cells, Flood)
                        LOG.info(str(COMID_to_Evaluate) + '  ' + str(E_Start) + ' vs ' + str(estart) + '  ' + str(E_End) + ' vs ' + str(eend))
                    
                    #Set the Low elevation anchor based on the Minimum elevation in the path
                    #LOG.info('E_Start=' + str(E_Start) + '  E_End=' + str(E_End) + '  E_Min_from_Path=' + str(E_Min_from_Path) )
                    if E_Start < E_End and E_Start>E_Min_from_Path:
                        LOG.info('Updated E_Start From ' + str(E_Start) + ' to ' + str(E_Min_from_Path) )
                        E_Start = E_Min_from_Path
                        #asdfasdf
                    if E_Start > E_End and E_End>E_Min_from_Path:
                        LOG.info('Updated E_End From ' + str(E_End) + ' to ' + str(E_Min_from_Path) )
                        E_End = E_Min_from_Path
                        #asdfasdf
                    
                    '''
                    if E_End<0.0:
                        LOG.info(str(COMID_to_Evaluate) + '   E_Start=' + str(E_Start) + '   E_End=' + str(E_End) + '   Dist=' + str(sum(D_List)))
                    else:
                        num_steps = len(R_List)
                        slope = (E_End - E_Start) / sum(D_List[0:num_steps-1])
                        E_Use = E_Start
                        Elev_Streams[R_List[0],C_List[0]] = E_Use
                        if num_steps>1:
                            for x in range(1,num_steps):
                                #E_Use = E_Use + slope*D_List[x-1]
                                E_Use = E_Start + slope * sum(D_List[0:x-1])
                                Elev_Streams[R_List[x],C_List[x]] = E_Use
                    '''
                    if E_End<0.0:
                        LOG.info(str(COMID_to_Evaluate) + '   E_Start=' + str(E_Start) + '   E_End=' + str(E_End) + '   Dist=' + str(sum(D_List)))
                    else:
                        
                        #if E_End < E_Start and E_Min_from_Path < E_End:
                        #    E_End = E_Min_from_Path
                        #if E_End > E_Start and E_Min_from_Path < E_Start:
                        #    E_Start = E_Min_from_Path
                        
                        num_steps = len(R_List)
                        slope = (E_End - E_Start) / sum(D_List[0:num_steps-1])
                        E_Use = E_Start
                        
                        E_Temp = np.zeros(num_steps)
                        E_Temp = E_Temp - 100.1
                        
                        #Set the First and Last Cells to the DEM Elevation
                        E_Temp[0] = E_Start
                        E_Temp[num_steps-1] = E_End
                        
                        #LOG.info('Slope = ' + str(slope))
                        
                        if num_steps>2:
                            #Go through the list
                            
                            #This is just for testing
                            #for x in range(0,num_steps-1):
                            #    E_Temp[x] = DEM[R_List[x],C_List[x]]
                            #LOG.info(E_Temp)
                            #E_Temp = np.zeros(num_steps)
                            #E_Temp = E_Temp - 100
                            #E_Temp[0] = E_Start
                            #E_Temp[num_steps-1] = E_End
                            
                            '''
                            #This effectively finds the low points along the stream network.
                            if slope<0.0:   #This means the slope should be downhill
                                running_val = E_Temp[0]
                                for x in range(1,num_steps-1):
                                    E_From_DEM = DEM[R_List[x],C_List[x]]
                                    #E_From_DEM = Adjust_Elevations_By_Perpendicular_Cells(DEM[R_List[x],C_List[x]], x, R_List, C_List, nrows, ncols, B, DEM, search_dist_perp_cells)
                                    if E_From_DEM < running_val and E_From_DEM>E_End:
                                        E_Temp[x] = E_From_DEM
                                        running_val = E_Temp[x]
                            elif slope>0.0:   #This means the slope should be uphill
                                running_val = E_Temp[0]
                                for x in range(1,num_steps-1):
                                    E_From_DEM = DEM[R_List[x],C_List[x]]
                                    #E_From_DEM = Adjust_Elevations_By_Perpendicular_Cells(DEM[R_List[x],C_List[x]], x, R_List, C_List, nrows, ncols, B, DEM, search_dist_perp_cells)
                                    if E_From_DEM > running_val and E_From_DEM<E_End:
                                        E_Temp[x] = E_From_DEM
                                        running_val = E_Temp[x]
                            else:
                                for x in range(1,num_steps-1):
                                    E_Temp[x] = E_Start  #No slope, so they all should be the same elevation
                            '''
                            
                            
                            #Find the Low Points in the Elevation Profile and set them almost like mini-anchors
                            if slope<0.0:   #This means the slope should be downhill
                                running_val = E_Temp[0]
                                for x in range(1,num_steps-1):
                                    E_From_DEM = DEM[R_List[x],C_List[x]]
                                    #E_From_DEM = Adjust_Elevations_By_Perpendicular_Cells(DEM[R_List[x],C_List[x]], x, R_List, C_List, nrows, ncols, B, DEM, search_dist_perp_cells)
                                    if E_From_DEM < running_val and E_From_DEM>E_End:
                                        E_Temp[x] = E_From_DEM
                                        running_val = E_Temp[x]
                            elif slope>0.0:   #This means the slope should be uphill
                                running_val = E_End
                                for x in range(num_steps-1,0,-1):
                                    E_From_DEM = DEM[R_List[x],C_List[x]]
                                    #E_From_DEM = Adjust_Elevations_By_Perpendicular_Cells(DEM[R_List[x],C_List[x]], x, R_List, C_List, nrows, ncols, B, DEM, search_dist_perp_cells)
                                    if E_From_DEM < running_val and E_From_DEM>E_Start:
                                        E_Temp[x] = E_From_DEM
                                        running_val = E_Temp[x]
                            else:
                                for x in range(1,num_steps-1):
                                    E_Temp[x] = E_Start  #No slope, so they all should be the same elevation
                            
                            #if slope>0.0:
                            #    LOG.info(E_Temp)
                            
                            #Now look for any cells that may need some interpolation
                            for x in range(1, num_steps - 1):
                                if E_Temp[x]<-99:
                                    i_start = x-1 
                                    i_end=x
                                    while(E_Temp[i_end]<-99):
                                        i_end = i_end+1
                                    #LOG.info(str(x) + '  ' + str(i_end))
                                    #LOG.info(str(E_Temp[x]) + '  ' + str(E_Temp[i_end]))
                                    slope_temp = (E_Temp[i_end] - E_Temp[i_start]) / sum(D_List[i_start:i_end])
                                    #E_Temp[x] = E_Temp[i_start] + slope_temp * D_List[x]
                                    for x_temp in range(x,i_end):
                                        E_Temp[x_temp] = E_Temp[x_temp-1] + slope_temp * D_List[x_temp]
                                    #LOG.info(str(E_Temp[x-1]) + '  ' + str(E_Temp[x]) +  '  ' + str(E_Temp[i_end]))
                            #LOG.info(E_Temp)
                            #Fill in the Elev_Streams Raster
                            for x in range(0,num_steps-1):
                                Elev_Streams[R_List[x],C_List[x]] = E_Temp[x] 
                            
                            #if slope>0.0:
                            #    LOG.info(E_Temp)
                            #    asdfasdf
                    '''
                    COMIDs_To_Evaluate_List = [760748000, 760743009, 760703075]
                    if COMID_to_Evaluate in COMIDs_To_Evaluate_List:
                        LOG.info(str(COMID_to_Evaluate) + '   E_Start=' + str(E_Start) + '   E_End=' + str(E_End) + '   Dist=' + str(sum(D_List)))
                        Elev_from_DEM = []
                        Elev_from_ModDEM = []
                        Dist_Along_Profile = []
                        for x in range(0,num_steps-1):
                            Elev_from_ModDEM.append(Elev_Streams[R_List[x],C_List[x]])
                            Elev_from_DEM.append(DEM[R_List[x],C_List[x]])
                            Dist_Along_Profile.append(sum(D_List[0:x]))
                        Plot_Elevation_Profiles(COMID_to_Evaluate, Dist_Along_Profile, Elev_from_DEM, Elev_from_ModDEM)
                    '''
                
                #Just to make sure the Anchor Points get an elevation
                #Elev_Streams[CON_r[i],CON_c[i]] = DEM[CON_r[i],CON_c[i]]
            
            Last_Ditch_Effort_To_Smooth_Stream_Bumps(Elev_Streams, nrows, ncols, CON_r, CON_c)
            
                
            print('Creating Elevation-Streams File...' + ElevStreamsName)
            Write_Output_Raster(ElevStreamsName, Elev_Streams, ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Float32)
        
        
        #Create a Merged Elevation Raster that has the updated stream data
        # (ES_R,ES_C) = Elev_Streams.nonzero()
        (ES_R,ES_C) = np.where(Elev_Streams > 0)
        
        #Elev_Streams shows the elevations of each of the streams.  r, c
        #E is the DEM with a 1-cell larger boundary.  r+1, c+1
        #Flood shows the cells that are likely flooded.  r+1, c+1
        ModifiedDEM_Name = os.path.join(Working_Folder, DEM_List[ddd])
        ModifiedDEM_Name = ModifiedDEM_Name.replace('.tif','_Clean.tif')
        ModifiedDEM_Name = ModifiedDEM_Name.replace('.img','_Clean.tif')
        ModifiedDEM = MergeStreamElevationsWithDEM(E, B, Flood, FloodImpact, Elev_Streams, ES_R, ES_C, ncols, nrows, TopWidthMax, dx, dy, COMID_Unique, COMID_to_ID, MinCOMID, COMID_Unique_TW, COMID_Unique_Depth, WeightBox, ElipseMask, TW)
        Write_Output_Raster(ModifiedDEM_Name, ModifiedDEM[1:nrows+1,1:ncols+1], ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Float32)

def Create_Folder(F):
    if not os.path.exists(F): 
        os.makedirs(F)
    return

def Create_FlowFile(MainFlowFile, FlowFileName, OutputID, Qparam):
    infile = open(MainFlowFile,'r')
    lines = infile.readlines()
    ls = lines[0].strip().split(',')
    q_val = 0
    c_val = 0
    for i in range(len(ls)):
        if ls[i]==Qparam:
            q_val=i
        if ls[i]==OutputID:
            c_val=i
    
    outfile = open(FlowFileName, 'w')
    outfile.write(OutputID + ',' + Qparam)
    
    for r in range(1,len(lines)):
        ls = lines[r].strip().split(',')
        out_str = '\n' + ls[c_val] + ',' + ls[q_val]
        outfile.write(out_str)
    outfile.close()
    return

if __name__ == "__main__":
    
    Watershed_List = ['NED_n39w090']
    
    
    
    for i in range(len(Watershed_List)):
        
        WatershedName = Watershed_List[i]
        
        OutputID = 'COMID'
        
        Q_Fraction = 0.10
        TopWidthPlausibleLimit = 600
        search_dist_for_min_elev = 10
        
        search_dist_perp_cells = 40
        
        StreamShapefile = 'StrmShp/streams_714_4269_NashvilleIL.shp'
        DEM_Folder = 'DEM/'
        DEM_List = ['NED_n39w090.img']
        STRM_File_List = ['STRM/NED_n39w090_STRM_Raster_Clean.tif']
        
        #Working_Folder = 'C:/Projects/2023_BathyTest/AdH_Tests/Mound_City_Example/DEM_Updated/'
        Working_Folder = 'DEM_Updated/'
        Create_Folder(Working_Folder)
        
        FlowParam = 'qout_mean'
        MainFlowFile = 'GeoGLoWS_Flow_Data_714JLG.csv'
        FlowFileName = 'FLOW/NED_n39w090_Flow_COMID_Q.txt'
        LOG.info('Creating initial flow input data using flow rates associated with ' + FlowParam)
        LOG.info('  Input flow file: ' + MainFlowFile)
        LOG.info('  Output flow file: ' + FlowFileName)
        Create_FlowFile(MainFlowFile, FlowFileName, OutputID, FlowParam)
        
        
        CurveParamFileName = 'VDT/NED_n39w090_CurveFile_Initial.csv'
        
        FloodMapName = 'FloodMap/NED_n39w090_ARC_Flood_Initial.tif'
        
        LOG.info(DEM_List)
        
        DEM_Cleaner_Program(OutputID, StreamShapefile, DEM_Folder, DEM_List, STRM_File_List, Working_Folder, FlowFileName, CurveParamFileName, FloodMapName, Q_Fraction, TopWidthPlausibleLimit, search_dist_for_min_elev, search_dist_perp_cells)
    
    
    