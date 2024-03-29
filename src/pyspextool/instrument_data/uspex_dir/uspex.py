import numpy as np
from astropy.io import fits
import re
import os

from pyspextool import config as setup
from pyspextool.fit.polyfit import image_poly
from pyspextool.io.check import check_parameter
from pyspextool.io.fitsheader import get_header_info
from pyspextool.io.fitsheader import average_header_info
from pyspextool.utils.arrays import idl_rotate
from pyspextool.utils.math import combine_flag_stack
from pyspextool.utils.split_text import split_text
from pyspextool.utils.loop_progress import loop_progress

def correct_amps(img):

    """
    To correct for bias voltage drift in an uSpeX FITS image

    Parameters
    ----------
    img : numpy array
        An uSpeX image

    Returns
    --------
    numpy.ndarray
        The uSpeX image with the bias variations "corrected".

    Notes
    -----
    There are 32 amplifiers that talk to 64 columns each.  The median
    intensity of the 64 reference pixels at the bottom of image are
    subtracted from all rows in the 64 columns.

    Example
    --------
    later

    """

    for i in range(0, 32):

        xl = 0+64*i
        xr = xl+63

        med = np.median(img[2044:2047+1, xl:xr+1])
        img[:, xl:(xr+1)] -= med

    return img    



def read_fits(files, lininfo, keywords=None, pair_subtract=False, rotate=0,
              linearity_correction=True, ampcor=False, verbose=False):

    """
    To read an upgraded SpeX FITS image file.

    Parameters
    ----------
    files : list of str
        A list of fullpaths to FITS files.

    lininfo : dict {'max':int,'bit':int}
        information to identify pixels beyond range of linearity correction

        'max' maximum value in DN
        'bit' the bit to set for pixels beyond `max`

    keywords : list of str, optional
        A list of FITS keyword to retain 

    pair_subtract : {False, True}, optional
        Set to pair subtract the images.  

    rotate : {0,1,2,3,4,5,6,7}, optional 
        Direction  Transpose?  Rotation Counterclockwise
        -------------------------------------------------

        0          No          None
        1          No          90 deg
        2          No          180 deg
        3          No          270 deg
        4          Yes         None
        5          Yes         90 deg
        6          Yes         180 deg
        7          Yes         270 deg

        The directions follow the IDL rotate function convention.
        
    linearity_correction : {True, False}, optional
        Set to correct for non-linearity.

    ampcor : {False, True}, optional
        Set to correct for amplifying drift (see uspexampcor.py)

    Returns
    --------
    tuple 
        The results are returned as (data,var,hdrinfo,bitmask) where
        data = the image(s) in DN/s
        var  = the variance image(s) in (DN/s)**2
        hdrinfo  = a list where element is a dict.  The key is the FITS 
        keyword and the value is a list consiting of the FITS value and FITS 
        comment.

    """
    # Get setup information

    naxis1 = 2048
    naxis2 = 2048

    nfiles = len(files)

#    dolincor = [0, 1][lincor is not None]

    # Correct for non-linearity?

    if linearity_correction is True:

        linearity_file = os.path.join(setup.state['instrument_path'],
                                      'uspex_lincorr.fits')
        lc_coeffs = fits.getdata(linearity_file)

    else:

        lc_coeffs = None

    # Get set up for linearity check

    lininfo = setup.state['linearity_info']

    bias_file = os.path.join(setup.state['instrument_path'],'uspex_bias.fits')
    
    hdul = fits.open(bias_file)
    divisor = hdul[0].header['DIVISOR']
    bias = hdul[0].data / divisor
    hdul.close()

    if pair_subtract is True:

        #  Check to make sure the right number of files

        if (nfiles % 2) != 0:

            print('mc_readuspexfits:  Not an even number of images.')
            sys.exit(1)

        else:

            nimages = int(nfiles / 2)

    else:

        nimages = nfiles

    # Make empty arrays

    data = np.empty((nimages, naxis2, naxis1))
    var = np.empty((nimages, naxis2, naxis1))
    hdrinfo = []
    bitmask = np.empty((nimages, naxis2, naxis1), dtype=np.int8)

    # Load the data

    if pair_subtract is True:

        # pair subtraction

        for i in range(0, nimages):

            if verbose is True:
                loop_progress(i, 0, nimages, message='Loading images...')

            a = load_data(files[i * 2], lininfo, bias, keywords=keywords,
                          ampcor=ampcor, lccoeffs=lc_coeffs)

            b = load_data(files[i * 2 + 1], lininfo, bias, keywords=keywords,
                          ampcor=ampcor, lccoeffs=lc_coeffs)

            combmask = combine_flag_stack(np.stack((a[3], b[3])),
                                          nbits=lininfo['bit'] + 1)

            data[i, :, :] = idl_rotate(a[0] - b[0], rotate)
            var[i, :, :] = idl_rotate(a[1] + b[1], rotate)
            bitmask[i, :, :] = idl_rotate(combmask, rotate)

            hdrinfo.append(a[2])
            hdrinfo.append(b[2])

    else:

        for i in range(0, nimages):

            if verbose is True:
                loop_progress(i, 0, nimages, message='Loading images...')            
            im, va, hd, bm = load_data(files[i], lininfo, bias,
                                       keywords=keywords, ampcor=ampcor,
                                       lccoeffs=lc_coeffs)

            data[i, :, :] = idl_rotate(im, rotate)
            var[i, :, :] = idl_rotate(va, rotate)
            bitmask[i, :, :] = idl_rotate(bm, rotate)

            hdrinfo.append(hd)

    return np.squeeze(data), np.squeeze(var), hdrinfo, np.squeeze(bitmask)


def load_data(file, lininfo, bias, keywords=None, ampcor=None, lccoeffs=None):

    readnoise = 12.0  # per single read
    gain = 1.5  # electrons per DN

    hdul = fits.open(file)
    hdul[0].verify('silentfix')  # this was needed for to correct hdr problems

    itime = hdul[0].header['ITIME']
    coadds = hdul[0].header['CO_ADDS']
    ndrs = hdul[0].header['NDR']
    readtime = hdul[0].header['TABLE_SE']
    divisor = hdul[0].header['DIVISOR']

    #  Get set up for error propagation and store total exposure time

    rdvar = (2. * readnoise ** 2) / ndrs / coadds / itime ** 2 / gain ** 2
    crtn = (1.0 - readtime * (ndrs ** 2 - 1.0) / 3. / itime / ndrs)

    #  Read images, get into units of DN.

    img_p = hdul[1].data / divisor
    img_s = hdul[2].data / divisor

    #  Check for linearity maximum
            
    mskp = ((img_p < (bias - lininfo['max'])) * 2 ** lininfo['bit']).astype(np.uint8)
    msks = ((img_s < (bias - lininfo['max'])) * 2 ** lininfo['bit']).astype(np.uint8)

    #  Combine the masks 

    bitmask = combine_flag_stack(np.stack((mskp, msks)),
                                 nbits=lininfo['bit'] + 1)

    #  Create the image

    img = img_p - img_s

    #  Correct for amplifier offsets

    if ampcor:
        img = correct_uspex_amp(img)

    #  Determine the linearity correction for the image

    if lccoeffs is not None:
        cor = image_poly(img, lccoeffs)
        cor = np.where(cor == 0, 1, cor)

        #  Now set the corrections to unity for pixels > lincormax

        cor = np.where(bitmask == 2 ** lininfo['bit'], 1, cor)

        #  Set black pixel corrections to unity as well.

        cor[:, 0:3 + 1] = 1.0
        cor[:, 2044:2047 + 1] = 1.0
        cor[0:3 + 1, :] = 1.0
        cor[2044:2047 + 1, :] = 1.0

        # Apply the corrections

        img /= cor

        # Delete unecessary files

        del cor, img_p, img_s

    # Create the actual image.
    # Convert image back to total DN for error propagation

    img = img * divisor

    # Compute the variance and the final image

    var = np.absolute(img) * crtn / ndrs / (coadds ** 2) / \
          (itime ** 2) / gain + rdvar 
    img = img / divisor / itime

    # Collect header information

    hdr = get_header(hdul[0].header, keywords=keywords)

    hdul.close()

    return [img, var, hdr, bitmask]


def get_header(hdr, keywords=None):
    # Grab keywords if requested

    if keywords:

        hdrinfo = get_header_info(hdr, keywords=keywords)

    else:

        hdrinfo = get_header_info(hdr)

    #  Grab require keywords and convert to standard Spextool keywords

    # Airmass 

    hdrinfo['AM'] = [hdr['TCS_AM'], ' Airmass']

    # Hour angle

    val = hdr['TCS_HA']
    m = re.search('[-]', '[' + val + ']')
    if not m:
        val = '+' + val.strip()
    hdrinfo['HA'] = [val, ' Hour angle (hours)']

    # Position Angle

    hdrinfo['PA'] = [hdr['POSANGLE'], ' Position Angle E of N (deg)']

    # Dec 

    val = hdr['TCS_DEC']
    m = re.search('[-]', '[' + val + ']')
    if not m:
        val = '+' + val.strip()
    hdrinfo['DEC'] = [val, ' Declination, FK5 J2000']

    # RA

    hdrinfo['RA'] = [hdr['TCS_RA'].strip(), ' Right Ascension, FK5 J2000']

    # COADDS, ITIME

    coadds = hdr['CO_ADDS']

    itime = hdr['ITIME']
    hdrinfo['ITIME'] = [itime, ' Integration time (sec)']
    hdrinfo['NCOADDS'] = [coadds, ' Number of COADDS']
    hdrinfo['IMGITIME'] = [coadds * itime,
                           ' Image integration time, NCOADDSxITIME (sec)']

    # Time

    hdrinfo['TIME'] = [hdr['TIME_OBS'].strip(), ' Observation time in UTC']

    # Date

    hdrinfo['DATE'] = [hdr['DATE_OBS'].strip(), ' Observation date in UTC']

    # MJD

    hdrinfo['MJD'] = [hdr['MJD_OBS'], ' Modified Julian date OBSDATE+TIME_OBS']

    # FILENAME

    hdrinfo['FILENAME'] = [hdr['IRAFNAME'].strip(), ' Filename']

    # MODE

    hdrinfo['MODE'] = [hdr['GRAT'].strip(), ' Instrument Mode']

    # INSTRUMENT

    hdrinfo['INSTR'] = ['SpeX', ' Instrument']

    # Now grab any user COMMENT

#    print(hdr['COMMENT'])
#    comment = str(hdr['COMMENT'])
#    comment = comment.split('=')[1]    
#    hdrinfo['USERCOM'] = [comment[2:-1], ' User comment']

    return hdrinfo
