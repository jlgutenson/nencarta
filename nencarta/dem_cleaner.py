"""
The DEM cleaner: smooths the stream channels of a DEM.

An initial flood map shows where each reach's channel is. The stream cells are given elevations that fall
steadily between reach ends and confluences, and those elevations are spread across the channel. The result is
written as ``<DEM>_Clean.tif``.
"""
import os
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit
from osgeo import gdal

from nencarta.logger import LOG


def convert_cell_size(dem_cell_size, dem_lower_left, dem_upper_right):
    """
    Return the cell width and height in metres, and the metres per map unit, for a DEM's cell size.

    A cell size above 0.5 is taken to be in metres already. Otherwise it is in degrees and is converted using the
    DEM's mean latitude, from ``dem_lower_left`` and ``dem_upper_right``.
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
    """Each reach's flow, read from a 'COMID,Q' CSV, in the order of COMID_Unique."""
    num_unique = len(COMID_Unique)
    COMID_Unique_Flow = np.zeros(num_unique)
    LOG.info(f'Opening and Reading {FlowFileName}')
    with open(FlowFileName,'r') as infile:
        lines = infile.readlines()
    
    num_lines = len(lines)
    for n in range(1,num_lines):
        (COMID, Q) = lines[n].strip().split(',')
        if int(COMID)>=MinCOMID and int(COMID)<=MaxCOMID:
            i = COMID_to_ID[int(COMID)-MinCOMID]
            COMID_Unique_Flow[i] = float(Q)
    return COMID_Unique_Flow

def Calculate_TW_D_ForEachCOMID_ARC(CurveParamFileName, COMID_Unique_Flow, COMID_Unique, COMID_to_ID, MinCOMID, MaxCOMID, Q_Fraction):
    """
    Each reach's mean top width and depth at Q_Fraction of its flow, from ARC's curve file, and the largest top
    width.
    """
    num_unique = len(COMID_Unique)
    COMID_Unique_TW = np.zeros(num_unique)
    COMID_Unique_Depth = np.zeros(num_unique)
    LOG.info(f'Opening and Reading {CurveParamFileName}')
    df = pd.read_parquet(CurveParamFileName) if Path(CurveParamFileName).suffix.endswith('.parquet') else pd.read_csv(CurveParamFileName)
    df = df[df['COMID'].between(MinCOMID, MaxCOMID) & df['depth_a'].gt(0) & df['depth_b'].gt(0) & df['tw_a'].gt(0) & df['tw_b'].gt(0)]
    
    # Map COMIDs -> compact IDs
    idx = COMID_to_ID[
        (df["COMID"].to_numpy(np.int64) - MinCOMID)
    ]

    # Remove any unmapped COMIDs (-1)
    valid = idx >= 0

    idx = idx[valid]

    depth_a = df["depth_a"].to_numpy()[valid]
    depth_b = df["depth_b"].to_numpy()[valid]
    tw_a    = df["tw_a"].to_numpy()[valid]
    tw_b    = df["tw_b"].to_numpy()[valid]

    # Q for each record
    Q = COMID_Unique_Flow[idx] * Q_Fraction

    # Hydraulic geometry
    depth = depth_a * np.power(Q, depth_b)
    tw    = tw_a    * np.power(Q, tw_b)

    # Aggregate by unique COMID index
    count = np.bincount(
        idx,
        minlength=len(COMID_Unique_Flow)
    )

    depth_sum = np.bincount(
        idx,
        weights=depth,
        minlength=len(COMID_Unique_Flow)
    )

    tw_sum = np.bincount(
        idx,
        weights=tw,
        minlength=len(COMID_Unique_Flow)
    )

    # Final means
    nonzero = count > 0

    COMID_Unique_Depth = np.zeros_like(COMID_Unique_Flow)
    COMID_Unique_TW    = np.zeros_like(COMID_Unique_Flow)

    COMID_Unique_Depth[nonzero] = (
        depth_sum[nonzero] / count[nonzero]
    )

    COMID_Unique_TW[nonzero] = (
        tw_sum[nonzero] / count[nonzero]
    )
    TopWidthMax = COMID_Unique_TW.max()
    LOG.info(f'Max TopWidth is {TopWidthMax}')

    return (COMID_Unique_TW, COMID_Unique_Depth, TopWidthMax)

    

def Read_Raster_GDAL(InRAST_Name):
    """Read a raster's first band, with its size, cell size, extent, mean latitude, geotransform and projection."""
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
    xll = geotransform[0]
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

def Write_Output_Raster(s_output_filename, raster_data, ncols, nrows, dem_geotransform, dem_projection, s_file_format, s_output_type, compression: str):   
    """Write an array as a single-band raster."""
    o_driver: gdal.Driver = gdal.GetDriverByName(s_file_format)  #Typically will be a GeoTIFF "GTiff"
    #o_metadata = o_driver.GetMetadata()
    
    # Construct the file with the appropriate data shape
    o_output_file: gdal.Dataset = o_driver.Create(s_output_filename, xsize=ncols, ysize=nrows, bands=1, eType=s_output_type, options=[f'COMPRESS={compression}'])

    # Get the first band (assuming a single-band raster)
    band: gdal.Band = o_output_file.GetRasterBand(1)

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


@njit(cache=True, nogil=True)
def FindStreamCharacteristics( E, B, COMID_to_ID, MinCOMID, RR, CC, num_comids):
    """Each reach's lowest and highest stream cell elevation."""
    E_Min = np.full(num_comids, 99999999.9, dtype=np.float64)
    E_Max = np.zeros(num_comids, dtype=np.float64)
    
    num_strm_cells = len(RR)
    for x in range(num_strm_cells):
        r=RR[x]
        c=CC[x]
        V = B[r,c]
        Elev = E[r,c]
        if V>0:
            i = COMID_to_ID[(V-MinCOMID)]
            
            E_Min[i] = min(E_Min[i], Elev) 
            E_Max[i] = max(E_Max[i], Elev) 

    return E_Min, E_Max

@njit(cache=True, nogil=True)
def Find_SEED_and_CONNECTIONS(RR, CC, B, E_Min, E_Max, COMID_to_ID, MinCOMID, nrows, ncols, SEED_Val, CON_Val, yur, xll, dx, dy):
    """
    Find the seed cells (reach ends, and stream cells on the raster's edge) and the connection cells (where
    reaches meet). Returns the seeds' rows and columns, and a raster marking seeds with ``SEED_Val`` and
    connections with ``CON_Val``.
    """

    ### Define the placeholder arrays ###
    SEED_r = []
    SEED_c = []

    SEED_MinElev=[]
    SEED_MaxElev=[]

    ### Create the connection matrix ###
    SEEDCONNECT = np.zeros((nrows, ncols))

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
            # Append values into the remaining holders
            SEED_r.append(r - 1)
            SEED_c.append(c - 1)
            if B[r, c] > 0:
                i_val = COMID_to_ID[(B[r, c] - MinCOMID)]
                SEED_MinElev.append(E_Min[i_val])
                SEED_MaxElev.append(E_Max[i_val])
                SEEDCONNECT[r - 1, c - 1] = SEED_Val
            else:
                pass

        # Handle complex connectivity by filling with flag value for later cleaning
        if n != n_same or n > 3:
            SEEDCONNECT[r - 1, c - 1] = CON_Val

    ### Return to the calling function ###
    return SEED_r, SEED_c, SEEDCONNECT


def Clean_Connections(SEEDCONNECT, B, nrows, ncols, ConnectVal, sd):
    """
    Merge connection cells within ``sd`` cells of each other into one cell near their centre. With ``sd`` of 0
    nothing is merged, and every marked cell is returned.
    """

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

@njit(cache=True, nogil=True)
def FindNext(r, c, ncols, RC, B, ANCHOR, INDEXVALS):
    #Trying to find a connection, but prefer at first prefer it is NOT and Acnhor and that it has the SAME COMID value
    """
    The next stream cell from (r, c), and the step's length: an unvisited neighbour on the same reach, else an
    unvisited anchor, else -9999s.
    """
    COMID_Current = B[r,c]
    for (rrr,ccc,d)in INDEXVALS:
        rtest = int(r+rrr)
        ctest = int(c+ccc)
        rc = rtest*(ncols+2)+ctest

        if rc not in RC and (B[rtest,ctest]==COMID_Current) and ANCHOR[rtest,ctest]<=0:
            return rtest, ctest, d

    #Can't find a connection to a similar stream type (COMID), therefore search for an ANCHOR
    for (rrr,ccc,d)in INDEXVALS:
        rtest = int(r+rrr)
        ctest = int(c+ccc)
        rc = rtest*(ncols+2)+ctest
        if rc not in RC and ANCHOR[rtest,ctest]>0:
            return rtest, ctest, d
    return -9999, -9999, 0

@njit(cache=True, nogil=True)
def FindPathToNextAnchor(r_start, c_start, ncols, ANCHOR, B, E, dx, dy, dz, num_limit):
    """
    Walk the stream from an anchor to the next one. Returns the cells (higher end first), the two anchors, their
    elevations, the lowest elevation on the way, and the step lengths.
    """
    R_List=[] 
    C_List=[]
    D_List=[]
    RC = set()
    
    INDEXVALS = [[1,0,dy],[-1,0,dy],[0,1,dx],[0,-1,dx],[1,1,dz],[1,-1,dz],[-1,-1,dz],[-1,1,dz]]
    
    E_Start = E[r_start, c_start]
    E_End = -9999.9
    r=r_start
    c=c_start
    A1 = r*(ncols+2)+c
    A2=-999
    
    R_List.append(r_start-1)
    C_List.append(c_start-1)
    RC.add(r_start*(ncols+2)+c_start)
    
    E_Min = 99999999.9
    
    n=0
    while r>0:
        n=n+1 
        if n>num_limit:
            return R_List, C_List, A1, A2, E_Start, E_End, E_Min, D_List
        (r_next, c_next, d) = FindNext(r, c, ncols, RC, B, ANCHOR, INDEXVALS)
        
        r=r_next
        c=c_next
        if r_next>0:
            R_List.append(r_next-1)
            C_List.append(c_next-1)
            RC.add(r_next*(ncols+2)+c_next)
            D_List.append(d)
            E_Min = min(E_Min, E[r_next][c_next])
        if r_next>0 and ANCHOR[r,c]>0:
            E_End = E[r,c]
            r=-9999
            A2 = r*(ncols+2)+c
            
        #Couldn't find a connecting stream, so we're done here.
        if r_next<0:
            if r > 0 and c > 0:
                E_End = E[r,c]
            r=-9999
            #A2 = r*(ncols+2)+c
            A2 = -9999

    #In reality, we want E_Start to be the higher elevation
    if E_Start > E_End:
        return R_List, C_List, A1, A2, E_Start, E_End, E_Min, D_List
    else:
        R_List.reverse()
        C_List.reverse()
        D_List.reverse()
        return R_List, C_List, A2, A1, E_End, E_Start, E_Min, D_List


@njit(cache=True, nogil=True)
def Adjust_Elevations_By_Perpendicular_Cells(Elevation, i, R_List, C_List, nrows, ncols, B, DEM, searchdist, Flood):
    """Lower an elevation to that of the lowest flooded cell beside the stream near cell i, within searchdist cells along it."""
    num_pnts = len(R_List)
        
    if i<=(num_pnts*0.5):
        sd = num_pnts-i
        sd = min(sd, searchdist)
        if sd>0:
            for s in range(i+1,i+sd):
                #STat
                dr = R_List[s] - R_List[i]
                dc = C_List[s] - C_List[i]
                
                #To get perpendicular, switch the r and c values and make one negative of what it was
                test_r = int(R_List[i] + dc)
                test_c = int(C_List[i] + int(-1*dr))
                #LOG.info(str(dr) + ' ' + str(dc) + '  ' + str(E_Start)  + '  Perpendicular Cells:  ' + str(test_r) + '  ' + str(test_c) + '  ' + str(DEM[test_r,test_c]))
                if test_r>=0 and test_r<nrows and test_c>=0 and test_c<ncols and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<Elevation and Flood[test_r,test_c]>0:
                    Elevation = DEM[test_r,test_c]
                
                test_r = int(R_List[0] + int(-1*dc))
                test_c = int(C_List[0] + dr)
                #LOG.info(str(dr) + ' ' + str(dc) + '  ' + str(E_Start)  + '  Perpendicular Cells:  ' + str(test_r) + '  ' + str(test_c) + '  ' + str(DEM[test_r,test_c]))
                if test_r>=0 and test_r<(nrows-1) and test_c>=0 and test_c<(ncols-1) and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<Elevation and Flood[test_r,test_c]>0:
                    Elevation = DEM[test_r,test_c]
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
                if test_r>=0 and test_r<nrows and test_c>=0 and test_c<ncols and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<Elevation and Flood[test_r,test_c]>0:
                    Elevation = DEM[test_r,test_c]
                
                test_r = int(R_List[0] + int(-1*dc))
                test_c = int(C_List[0] + dr)
                if test_r>=0 and test_r<(nrows-1) and test_c>=0 and test_c<(ncols-1) and DEM[test_r,test_c]>-99 and DEM[test_r,test_c]<Elevation and Flood[test_r,test_c]>0:
                    Elevation = DEM[test_r,test_c]

    return Elevation

def CreateWeightAndElipseMask(TW, dx, dy):
    """An inverse distance squared weight box, and an elliptical mask for every radius up to TW cells."""
    ElipseMask = np.zeros((TW+1,int(TW*2+1),int(TW*2+1)))  #3D Array
    WeightBox = np.zeros((int(TW*2+1),int(TW*2+1)))  #2D Array
    for i in range(1,TW+1):
        TWDX = i*dx*i*dx
        TWDY = i*dy*i*dy
        for r in range(i+1):
            for c in range(i+1):
                is_elipse = (c*dx*c*dx/(TWDX)) + (r*dy*r*dy/(TWDY))   #https://www.mathopenref.com/coordgeneralellipse.html
                if is_elipse<=1.0:
                    ElipseMask[i,TW+r,TW+c] = 1.0
                    ElipseMask[i,TW-r,TW+c] = 1.0
                    ElipseMask[i,TW+r,TW-c] = 1.0
                    ElipseMask[i,TW-r,TW-c] = 1.0
    #LOG.info(ElipseMask[2,TW-4:TW+4+1,TW-4:TW+4+1].astype(int))
    #LOG.info(ElipseMask[10,TW-14:TW+14+1,TW-14:TW+14+1].astype(int))
    #LOG.info(ElipseMask[40,TW-44:TW+44+1,TW-44:TW+44+1].astype(int))
    
    for r in range(TW+1):
        for c in range(TW+1):
            z = pow((c*dx*c*dx + r*dy*r*dy), 0.5)
            if z<0.0001:
                z=0.001
            WeightBox[TW+r,TW+c] = 1 / (z*z)
            WeightBox[TW-r,TW+c] = 1 / (z*z)
            WeightBox[TW+r,TW-c] = 1 / (z*z)
            WeightBox[TW-r,TW-c] = 1 / (z*z)
    
    return WeightBox, ElipseMask

@njit(cache=True, nogil=True)
def CreateFloodImpactMap(Flood, B, nrows, ncols, TW_m, dx, dy, COMID_to_ID, MinCOMID, COMID_Unique_TW, ElipseMask, TW):
    """For each flooded cell, the reach most likely to flood it: the one with the most stream cells within its top width."""
    FloodImpact = np.zeros((nrows+2,ncols+2))
    
    #Find all the flooded cells
    (Flood_R, Flood_C)= np.where(Flood > 0)

    num_flooded_cells = len(Flood_R)

    for i in range(num_flooded_cells):
        r = Flood_R[i]
        c = Flood_C[i]
        r_use = r
        c_use = c
        
        #Find the maximum possible search radius we need to evaluate.
        #This is for the raster
        r_min = r_use-TW
        r_max = r_use+TW+1
        r_min = max(r_min, 1) 
        r_max = min(r_max, nrows+1)
        c_min = c_use-TW
        c_max = c_use+TW+1
        c_min = max(c_min, 1) 
        c_max = min(c_max, ncols+1)
        
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
        # Subset_Unique = np.delete(Subset_Unique, 0)  #We don't need the first entry of zero
        COMID_to_Print = 0
        if len(Subset_Unique)<1:
            # LOG.info('WE HAVE ISSUES, SHOULD HAVE AT LEAST ONE VALUE HERE')
            COMID_to_Print = 0
        elif len(Subset_Unique)==1:
            COMID_to_Print = Subset_Unique[0]
        else:
            COMID_Instance_Count = 0
            for COMID_V in Subset_Unique:
                COMID_Value = int(COMID_V)
                iii = COMID_to_ID[COMID_Value - MinCOMID]
                COMID_TW_m = COMID_Unique_TW[iii]
                COMID_TW_m = min(COMID_TW_m, TW_m)
                COMID_TW = round(max(COMID_TW_m/dx,COMID_TW_m/dy))  #This is how many cells we will be looking at surrounding our stream cell
                if COMID_TW<=1:
                    COMID_TW=2
                
                #Now we want to go smaller based on the TW of the specific COMID.  Do we still see impact from those.
                #Note that the these now use COMID_TW, not TW
                try:
                    Subset_Flood_Raster_Potential_Influence = B[r_min:r_max,c_min:c_max] * ElipseMask[COMID_TW, w_r_min:w_r_max,w_c_min:w_c_max]
                except:  # noqa: E722, S112
                    # I do not know why this sometimes fails. TODO
                    continue
                
                count_for_comid = np.count_nonzero(Subset_Flood_Raster_Potential_Influence == COMID_Value)
                if count_for_comid > COMID_Instance_Count:
                    COMID_to_Print = COMID_Value
                    COMID_Instance_Count = count_for_comid
                
            
        #Find R and C for each one to see if they are even within distance to influence this flooded cell.
        
        #Find cell that has impact the most times and record it in the 'FloodImpact' Raster.
        FloodImpact[r,c] = COMID_to_Print

    return FloodImpact

@njit(cache=True, nogil=True)
def MergeStreamElevationsWithDEM(E, B, FloodImpact, Elev_Streams, ES_R, ES_C, ncols, nrows, TW_m, dx, dy, COMID_to_ID, MinCOMID, COMID_Unique_TW, WeightBox, ElipseMask, TW):
    
    """Spread each reach's new stream elevations across the cells it floods, weighted by distance, keeping the DEM elsewhere."""
    Elev_Times_Weight = np.zeros((nrows+2,ncols+2), dtype=np.float32)
    Total_Weight = np.zeros((nrows+2,ncols+2), dtype=np.float32)
    
    #This will further limit the flooding to only the cells that are flooded by the corresponding COMID Value
    FloodBigImpact = np.zeros((nrows+2,ncols+2), dtype=np.float32)
    
    num_nonzero = len(ES_R)
    for i in range(num_nonzero):
        r_use = ES_R[i] + 1  #Need the +1 because ES_R and ES_C are based on the standard raster, not the raster with 1-cell added on each side.
        c_use = ES_C[i] + 1
        
        #Get COMID, TopWidth, and Depth Information for this cell
        COMID_Value = int(B[r_use,c_use])
        if COMID_Value > 0:
            iii = COMID_to_ID[COMID_Value - MinCOMID]
            COMID_TW_m = COMID_Unique_TW[iii]
            COMID_TW_m = min(COMID_TW_m, TW_m)
            COMID_TW = int(max(round(COMID_TW_m/dx,0),round(COMID_TW_m/dy,0)))  #This is how many cells we will be looking at surrounding our stream cell
            if COMID_TW<=1:
                COMID_TW=2
            
            #Now start with rows and start evaluating all surrounding cells
            ELEV = float(Elev_Streams[ES_R[i],ES_C[i]])
            r_min = r_use-COMID_TW
            r_max = r_use+COMID_TW+1
            r_min = max(r_min, 1) 
            r_max = min(r_max, nrows+1)
            c_min = c_use-COMID_TW
            c_max = c_use+COMID_TW+1
            c_min = max(c_min, 1) 
            c_max = min(c_max, ncols+1)
            
            #This uses the weighting method from FloodSpreader to create a flood map
            #   Here we use TW instead of COMID_TW.  This is because we are trying to find the center of the weight raster, which was set based on TW (not COMID_TW).  COMID_TW mainly applies to the r_min, r_max, c_min, c_max
            w_r_min = TW-(r_use-r_min)
            w_r_max = TW+r_max-r_use
            w_c_min = TW-(c_use-c_min)
            w_c_max = TW+c_max-c_use
            
            
            #Create an Impact multiplier that is 1 if the cell is flooded by the current COMID, and 0 if it is not
            #FloodBigImpact[r_min:r_max,c_min:c_max].fill(0)  #Sets all the values to 0
            FloodBigImpact[r_min:r_max,c_min:c_max] = np.where(FloodImpact[r_min:r_max,c_min:c_max]==COMID_Value, 1.0, 0.0)
            
            
            #The FloodBig basically clips any analysis to only the cells that are considered flooded.
            #  OIn 2/7/2024 I started using 'FloodBigImpact' instead of 'FloodBig'
            try:
                Elev_Times_Weight[r_min:r_max,c_min:c_max] = Elev_Times_Weight[r_min:r_max,c_min:c_max] + ELEV * WeightBox[w_r_min:w_r_max,w_c_min:w_c_max] * FloodBigImpact[r_min:r_max,c_min:c_max] * ElipseMask[COMID_TW, w_r_min:w_r_max,w_c_min:w_c_max]
                Total_Weight[r_min:r_max,c_min:c_max] = Total_Weight[r_min:r_max,c_min:c_max] + WeightBox[w_r_min:w_r_max,w_c_min:w_c_max] * FloodBigImpact[r_min:r_max,c_min:c_max] * ElipseMask[COMID_TW, w_r_min:w_r_max,w_c_min:w_c_max]
            except:  # noqa: E722, S112
                # Again, I do not know why this sometimes fails. TODO
                continue

    Elev_divided_by_weight = np.where(Total_Weight != 0, Elev_Times_Weight / Total_Weight, 0.0).astype(np.float32)

    #If a cell is in the channel (determined by Flood raster) then use the weighted stream elevation, otherwise use the DEM data (E)
    ModifiedDEM = np.where(Elev_divided_by_weight>0.0,Elev_divided_by_weight,E)
    return ModifiedDEM

@njit(cache=True, nogil=True)
def Last_Ditch_Effort_To_Smooth_Stream_Bumps(Elev_Streams, nrows, ncols, CON_r, CON_c):
    """Lower each connection cell to its lowest neighbouring stream cell."""
    num_to_evaluate = len(CON_r)
    
    for x in range(num_to_evaluate):
        for r in range(CON_r[x]-1, CON_r[x]+2):
            for c in range(CON_c[x]-1, CON_c[x]+2):
                if r>=0 and r<nrows and c>=0 and c<ncols and Elev_Streams[r,c]>0.0:
                    Elev_Streams[CON_r[x],CON_c[x]] = min(Elev_Streams[CON_r[x],CON_c[x]], Elev_Streams[r,c])

@njit(cache=True, nogil=True)
def compute_stream_elevations(
    DEM,
    B,
    A_R,
    A_C,
    A_RC,
    num_anchors,
    nrows,
    ncols,
    search_dist_perp_cells,
    CON_r,
    CON_c,
    Flood,
    dx,
    dy,
    dz,
    ANCHOR,
    E):
    """The new stream cell elevations: from each anchor to the next, falling steadily, through lower DEM cells along the way."""
    Elev_Streams = np.zeros((nrows,ncols))
    #Starting at each Anchor Point, Find the Path to the next Anchor point
    for i in range(num_anchors):
        Elev_Streams[A_R[i],A_C[i]] = DEM[A_R[i],A_C[i]]
        if A_RC[i]!=0:
            (R_List, C_List, _, _, E_Start, E_End, E_Min_from_Path, D_List) = FindPathToNextAnchor(A_R[i]+1, A_C[i]+1, ncols, ANCHOR, B, E, dx, dy, dz, 9999)
            
            #Adjust the Start and Ending Elevations
            if search_dist_perp_cells>0:
                E_Start = Adjust_Elevations_By_Perpendicular_Cells(E_Start, 0, R_List, C_List, nrows, ncols, B, DEM, search_dist_perp_cells, Flood)
                E_End = Adjust_Elevations_By_Perpendicular_Cells(E_End, (len(R_List)-1), R_List, C_List, nrows, ncols, B, DEM, search_dist_perp_cells, Flood)
            
            #Set the Low elevation anchor based on the Minimum elevation in the path
            if E_Start < E_End and E_Start>E_Min_from_Path:
                E_Start = E_Min_from_Path
            if E_Start > E_End>E_Min_from_Path:
                E_End = E_Min_from_Path
            
            if E_End >= 0:
                num_steps = len(R_List)
                slope = (E_End - E_Start) / sum(D_List[0:num_steps-1])
                
                E_Temp = np.zeros(num_steps)
                E_Temp = E_Temp - 100.1
                
                #Set the First and Last Cells to the DEM Elevation
                E_Temp[0] = E_Start
                E_Temp[num_steps-1] = E_End
                

                if num_steps>2:                 
                    #Find the Low Points in the Elevation Profile and set them almost like mini-anchors
                    if slope<0.0:   #This means the slope should be downhill
                        running_val = E_Temp[0]
                        for x in range(1,num_steps-1):
                            E_From_DEM = DEM[R_List[x],C_List[x]]
                            if E_From_DEM < running_val and E_From_DEM>E_End:
                                E_Temp[x] = E_From_DEM
                                running_val = E_Temp[x]
                    elif slope>0.0:   #This means the slope should be uphill
                        running_val = E_End
                        for x in range(num_steps-1,0,-1):
                            E_From_DEM = DEM[R_List[x],C_List[x]]
                            if E_From_DEM < running_val and E_From_DEM>E_Start:
                                E_Temp[x] = E_From_DEM
                                running_val = E_Temp[x]
                    else:
                        for x in range(1,num_steps-1):
                            E_Temp[x] = E_Start  #No slope, so they all should be the same elevation
                    
                    #Now look for any cells that may need some interpolation
                    for x in range(1, num_steps - 1):
                        if E_Temp[x]<-99:
                            i_start = x-1 
                            i_end=x
                            while(E_Temp[i_end]<-99):
                                i_end = i_end+1

                            slope_temp = (E_Temp[i_end] - E_Temp[i_start]) / sum(D_List[i_start:i_end])
                            for x_temp in range(x,i_end):
                                E_Temp[x_temp] = E_Temp[x_temp-1] + slope_temp * D_List[x_temp]

                    for x in range(num_steps-1):
                        Elev_Streams[R_List[x],C_List[x]] = E_Temp[x] 

    
    Last_Ditch_Effort_To_Smooth_Stream_Bumps(Elev_Streams, nrows, ncols, CON_r, CON_c)

    return Elev_Streams

def DEM_Cleaner_Program(OutputID, 
                        StreamShapefile: os.PathLike, 
                        DEM_Folder: os.PathLike, 
                        DEM_List: list[str], 
                        STRM_File_List: list[str], 
                        Working_Folder: os.PathLike, 
                        FlowFileName: os.PathLike, 
                        CurveParamFileName: os.PathLike, 
                        FloodMapName: os.PathLike, 
                        Q_Fraction, 
                        TopWidthPlausibleLimit, 
                        compression: str,
                        search_dist_perp_cells):
    """
    Clean each DEM in ``DEM_List`` (file names in ``DEM_Folder``), writing ``<DEM>_Clean.tif`` and its
    intermediate rasters to ``Working_Folder``.

    ``STRM_File_List`` gives each DEM's stream raster, ``FlowFileName`` the flow of each reach, and
    ``CurveParamFileName`` ARC's curve file, from which each reach's top width and depth are estimated at
    ``Q_Fraction`` of that flow, capped at ``TopWidthPlausibleLimit`` metres. ``FloodMapName`` is the initial flood
    map that outlines the channels. ``OutputID`` and ``StreamShapefile`` are not used.
    """
    for ddd in range(len(DEM_List)):
        LOG.info('Working on Site ' + DEM_List[ddd])

        DEM_File = os.path.join(DEM_Folder, DEM_List[ddd])
        STRM_File_Clean = STRM_File_List[ddd]
        (S, ncols, nrows, cellsize, yll, yur, xll, _, _, _, _) = Read_Raster_GDAL(STRM_File_Clean)
        
        #Open DEM File
        (DEM, ncols, nrows, cellsize, yll, yur, xll, _, _, dem_geotransform, dem_projection) = Read_Raster_GDAL(DEM_File)
        E = np.zeros((nrows+2,ncols+2), dtype=np.float32)  #Create an array that is slightly larger than the STRM Raster Array
        E[1:(nrows+1), 1:(ncols+1)] = DEM
        
        #Get Cellsize Information
        (dx, dy, _) = convert_cell_size(cellsize, yll, yur)
        dz = pow(dx*dx+dy*dy,0.5)
        
        #Get list of Uniqe Stream IDs.  Also find where all the cell values are.
        B = np.zeros((nrows+2,ncols+2), dtype=np.int32)  #Create an array that is slightly larger than the STRM Raster Array
        B[1:(nrows+1), 1:(ncols+1)] = S
        (RR,CC) = np.where(B > 0)
        
        COMID_Unique = np.unique(B)
        COMID_Unique = COMID_Unique[np.where(COMID_Unique > 0)]
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
        (COMID_Unique_TW, COMID_Unique_Depth, TopWidthMax) = Calculate_TW_D_ForEachCOMID_ARC(CurveParamFileName, COMID_Unique_Flow, COMID_Unique, COMID_to_ID, MinCOMID, MaxCOMID, Q_Fraction)
        LOG.info('Maximum Top Width = ' + str(TopWidthMax))
        for x in range(len(COMID_Unique)):
            if COMID_Unique_TW[x]>TopWidthPlausibleLimit:
                LOG.info('Ignoring ' + str(COMID_Unique[x]) + '  ' + str(COMID_Unique_Flow[x])  + '  ' + str(COMID_Unique_Flow[x]*Q_Fraction) + '  ' + str(COMID_Unique_Depth[x]) + '  ' + str(COMID_Unique_TW[x]))             
        
        TopWidthMax = min(TopWidthMax, TopWidthPlausibleLimit)
        
        #Create a Weight Box and an Elipse Mask that can be used for all of the cells
        X_cells = round(TopWidthMax/dx,0)
        Y_cells = round(TopWidthMax/dy,0)
        TW = int(max(Y_cells,X_cells))  #This is how many cells we will be looking at surrounding our stream cell
        
        #Create a Weight and an Elipse Mask
        (WeightBox, ElipseMask) = CreateWeightAndElipseMask(TW, dx, dy)  #3D Array with the same row/col dimensions as the WeightBox
        
        #Create a simple Flood Map
        if os.path.isfile(FloodMapName):
            LOG.info(f'Using Flood Map: {FloodMapName}')
            Flood = np.zeros((nrows+2,ncols+2))
            (Flood[1:nrows+1,1:ncols+1], ncols, nrows, cellsize, yll, yur, xll, _, _, _, dem_projection) = Read_Raster_GDAL(FloodMapName)
        else:
            LOG.info('YOU NEED TO CREATE AN INITIAL FLOOD MAP!!!')
            return

        #If warranted, write a Flood Impact File
        FloodImpact_File = os.path.join(Working_Folder, f'FLOOD_IMPACT_{DEM_List[ddd]}')
        if os.path.isfile(FloodImpact_File):
            LOG.info(f'Using Existing Flood Map: {FloodImpact_File}')
            FloodImpact = np.zeros((nrows+2,ncols+2))
            (FloodImpact[1:nrows+1,1:ncols+1], ncols, nrows, cellsize, yll, yur, xll, _, _, _, dem_projection) = Read_Raster_GDAL(FloodImpact_File)
        else:
            LOG.info(f'Creating Flood Impact Map: {FloodImpact_File}')
            COMID_Unique_TW_Reduced = COMID_Unique_TW * 0.75
            FloodImpact = CreateFloodImpactMap(Flood, B, nrows, ncols, TopWidthMax, dx, dy, COMID_to_ID, MinCOMID, COMID_Unique_TW_Reduced, ElipseMask, TW)
            Write_Output_Raster(FloodImpact_File, FloodImpact[1:nrows+1,1:ncols+1], ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Int32, compression)
        
        
        LOG.info('Finding Stream Data: Length and Elevation Data')
        E_Min, E_Max = FindStreamCharacteristics(E, B, COMID_to_ID, MinCOMID, RR, CC, num_comids)
        
        
        #Find SEED locations as well as where connections between streams occur.
        LOG.info('Finding SEED and Connection Locations')
        SEED_Val = 1 
        CON_Val = 2
        (SEED_r, SEED_c, SEEDCONNECT) = Find_SEED_and_CONNECTIONS(RR, CC, B, E_Min, E_Max, COMID_to_ID, MinCOMID, nrows, ncols, SEED_Val, CON_Val, yur, xll, dx, dy) 
        (SEEDCONNECT, CON_r, CON_c) = Clean_Connections(SEEDCONNECT, B, nrows, ncols, CON_Val, 0)
        SEED_CONNECT_FILE = os.path.join(Working_Folder, f'SEED_CONNECT_{DEM_List[ddd]}')
        if os.path.isfile(SEED_CONNECT_FILE):
            LOG.info(f'File Already Exists: {SEED_CONNECT_FILE}')
        else:
            Write_Output_Raster(SEED_CONNECT_FILE, SEEDCONNECT, ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Byte, compression)

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
        
        #Create Elevation Streams File
        ElevStreamsName = os.path.join(Working_Folder, f'Elev_Streams_{DEM_List[ddd]}')
        if os.path.isfile(ElevStreamsName):
            LOG.info(f'Using Existing Elevation-Streams File: {ElevStreamsName}')
            (Elev_Streams, ncols, nrows, cellsize, yll, yur, xll, _, _, _, dem_projection) = Read_Raster_GDAL(ElevStreamsName)
        else:
            Elev_Streams = compute_stream_elevations(DEM, B, A_R, A_C, A_RC, num_anchors, nrows, ncols, search_dist_perp_cells, CON_r, CON_c, Flood, dx, dy, dz, ANCHOR, E)
            
            LOG.info('Creating Elevation-Streams File...' + ElevStreamsName)
            Write_Output_Raster(ElevStreamsName, Elev_Streams, ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Float32, compression)
        
        #Create a Merged Elevation Raster that has the updated stream data
        (ES_R,ES_C) = np.where(Elev_Streams > 0)
        
        #Elev_Streams shows the elevations of each of the streams.  r, c
        #E is the DEM with a 1-cell larger boundary.  r+1, c+1
        #Flood shows the cells that are likely flooded.  r+1, c+1
        ModifiedDEM_Name = os.path.join(Working_Folder, DEM_List[ddd])
        ModifiedDEM_Name = ModifiedDEM_Name.replace('.tif','_Clean.tif')
        ModifiedDEM_Name = ModifiedDEM_Name.replace('.img','_Clean.tif')
        ModifiedDEM = MergeStreamElevationsWithDEM(E, B, FloodImpact, Elev_Streams, ES_R, ES_C, ncols, nrows, TopWidthMax, dx, dy, COMID_to_ID, MinCOMID, COMID_Unique_TW, WeightBox, ElipseMask, TW)
        Write_Output_Raster(ModifiedDEM_Name, ModifiedDEM[1:nrows+1,1:ncols+1], ncols, nrows, dem_geotransform, dem_projection, "GTiff", gdal.GDT_Float32, compression)
