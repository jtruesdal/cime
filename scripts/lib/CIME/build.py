"""
functions for building CIME models
"""
import glob, shutil, time, threading, subprocess, imp
from CIME.XML.standard_module_setup  import *
from CIME.utils                 import get_model, analyze_build_log, stringify_bool, run_and_log_case_status, get_timestamp, run_sub_or_cmd, run_cmd, get_batch_script_for_job, gzip_existing_file, safe_copy, check_for_python, get_logging_options
from CIME.provenance            import save_build_provenance as save_build_provenance_sub
from CIME.locked_files          import lock_file, unlock_file
from CIME.XML.files             import Files

logger = logging.getLogger(__name__)

_CMD_ARGS_FOR_BUILD = \
    ("CASEROOT", "CASETOOLS", "CIMEROOT", "COMP_INTERFACE",
     "COMPILER", "DEBUG", "EXEROOT", "INCROOT", "LIBROOT",
     "MACH", "MPILIB", "NINST_VALUE", "OS", "PIO_VERSION",
     "SHAREDLIBROOT", "SMP_PRESENT", "USE_ESMF_LIB", "USE_MOAB",
     "CAM_CONFIG_OPTS", "COMP_LND", "COMPARE_TO_NUOPC", "HOMME_TARGET",
     "OCN_SUBMODEL", "CISM_USE_TRILINOS", "USE_TRILINOS", "USE_ALBANY", "USE_PETSC")

def get_standard_makefile_args(case, shared_lib=False):
    make_args = "CIME_MODEL={} ".format(case.get_value("MODEL"))
    make_args += " SMP={} ".format(stringify_bool(case.get_build_threaded()))
    expect(not (uses_kokkos(case) and not shared_lib), "Kokkos is not supported for classic Makefile build system")
    for var in _CMD_ARGS_FOR_BUILD:
        make_args += xml_to_make_variable(case, var)

    return make_args

def get_standard_cmake_args(case, sharedpath, shared_lib=False):
    cmake_args = "-DCIME_MODEL={} ".format(case.get_value("MODEL"))
    cmake_args += " -Dcompile_threaded={} ".format(stringify_bool(case.get_build_threaded()))

    ocn_model = case.get_value("COMP_OCN")
    atm_model = case.get_value("COMP_ATM")
    if ocn_model == 'mom' or atm_model == "fv3gfs":
        cmake_args += " -DUSE_FMS=TRUE "

    cmake_args += " -DINSTALL_SHAREDPATH={} ".format(os.path.join(case.get_value("EXEROOT"), sharedpath))

    if not shared_lib:
        cmake_args += " -DUSE_KOKKOS={} ".format(stringify_bool(uses_kokkos(case)))
        comps = [case.get_value("COMP_{}".format(comp_class)) for comp_class in case.get_values("COMP_CLASSES")]
        cmake_args += " -DCOMP_NAMES='{}' ".format(";".join(comps))

    for var in _CMD_ARGS_FOR_BUILD:
        cmake_args += xml_to_make_variable(case, var, cmake=True)

    if atm_model == "scream":
        cmake_args += xml_to_make_variable(case, "HOMME_TARGET", cmake=True)

    # Disable compiler checks
    cmake_args += " -DCMAKE_C_COMPILER_WORKS=1 -DCMAKE_CXX_COMPILER_WORKS=1 -DCMAKE_Fortran_COMPILER_WORKS=1"

    return cmake_args

def xml_to_make_variable(case, varname, cmake=False):
    varvalue = case.get_value(varname)
    if varvalue is None:
        return ""
    if type(varvalue) == type(True):
        varvalue = stringify_bool(varvalue)
    return "{}{}=\"{}\" ".format("-D" if cmake else "", varname, varvalue)

###############################################################################
def uses_kokkos(case):
###############################################################################
    cam_target = case.get_value("CAM_TARGET")
    # atm_comp   = case.get_value("COMP_ATM") # scream does not use the shared kokkoslib for now

    return get_model() == "e3sm" and cam_target in ("preqx_kokkos", "theta-l")

###############################################################################
def _build_model(build_threaded, exeroot, incroot, complist,
                 lid, caseroot, cimeroot, compiler, buildlist, comp_interface):
###############################################################################
    logs = []

    thread_bad_results = []
    for model, comp, nthrds, _, config_dir in complist:
        if buildlist is not None and model.lower() not in buildlist:
            continue

        # aquap has a dependency on atm so we will build it after the threaded loop
        if comp == "aquap":
            logger.debug("Skip aquap ocn build here")
            continue

        # coupler handled seperately
        if model == "cpl":
            continue

        # special case for clm
        # clm 4_5 and newer is a shared (as in sharedlibs, shared by all tests) library
        # (but not in E3SM) and should be built in build_libraries
        if get_model() != "e3sm" and comp == "clm":
            continue
        else:
            logger.info("         - Building {} Library ".format(model))

        smp = nthrds > 1 or build_threaded

        bldroot = os.path.join(exeroot, model, "obj")
        libroot = os.path.join(exeroot, "lib")
        file_build = os.path.join(exeroot, "{}.bldlog.{}".format(model, lid))
        logger.debug("bldroot is {}".format(bldroot))
        logger.debug("libroot is {}".format(libroot))

        # make sure bldroot and libroot exist
        for build_dir in [bldroot, libroot]:
            if not os.path.exists(build_dir):
                os.makedirs(build_dir)

        # build the component library
        # thread_bad_results captures error output from thread (expected to be empty)
        # logs is a list of log files to be compressed and added to the case logs/bld directory
        t = threading.Thread(target=_build_model_thread,
            args=(config_dir, model, comp, caseroot, libroot, bldroot, incroot, file_build,
                  thread_bad_results, smp, compiler))
        t.start()

        logs.append(file_build)

    # Wait for threads to finish
    while(threading.active_count() > 1):
        time.sleep(1)

    expect(not thread_bad_results, "\n".join(thread_bad_results))

    #
    # Now build the executable
    #

    if not buildlist:
        cime_model = get_model()
        file_build = os.path.join(exeroot, "{}.bldlog.{}".format(cime_model, lid))

        ufs_driver = os.environ.get("UFS_DRIVER")
        if cime_model == 'ufs' and ufs_driver == 'nems':
            config_dir = os.path.join(cimeroot,os.pardir,"src","model","NEMS","cime","cime_config")
        else:
            files = Files(comp_interface=comp_interface)
            if comp_interface == "nuopc":
                config_dir = os.path.join(os.path.dirname(files.get_value("BUILD_LIB_FILE",{"lib":"CMEPS"})))
            else:
                config_dir = os.path.join(cimeroot,"src","drivers","mct","cime_config")

        expect(os.path.exists(config_dir), "Config directory not found {}".format(config_dir))
        if "cpl" in complist:
            bldroot = os.path.join(exeroot, "cpl", "obj")
            if not os.path.isdir(bldroot):
                os.makedirs(bldroot)
        logger.info("Building {} from {}/buildexe with output to {} ".format(cime_model, config_dir, file_build))

        with open(file_build, "w") as fd:
            stat = run_cmd("{}/buildexe {} {} {} "
                       .format(config_dir, caseroot, libroot, bldroot),
                       from_dir=bldroot,  arg_stdout=fd,
                       arg_stderr=subprocess.STDOUT)[0]

        analyze_build_log("{} exe".format(cime_model), file_build, compiler)
        expect(stat == 0, "BUILD FAIL: buildexe failed, cat {}".format(file_build))

        # Copy the just-built ${MODEL}.exe to ${MODEL}.exe.$LID
        safe_copy("{}/{}.exe".format(exeroot, cime_model), "{}/{}.exe.{}".format(exeroot, cime_model, lid))

        logs.append(file_build)

    return logs

###############################################################################
def _build_model_cmake(exeroot, complist, lid, cimeroot, buildlist,
                       comp_interface, sharedpath, separate_builds, ninja, dry_run, timing, case):
###############################################################################
    cime_model = get_model()
    bldroot    = os.path.join(exeroot, "cmake-bld")
    libroot    = os.path.join(exeroot, "lib")
    bldlog     = os.path.join(exeroot, "{}.bldlog.{}".format(cime_model, lid))
    srcroot    = case.get_value("SRCROOT")
    gmake_j    = 1 if timing else case.get_value("GMAKE_J")
    gmake      = case.get_value("GMAKE")

    # make sure bldroot and libroot exist
    for build_dir in [bldroot, libroot]:
        if not os.path.exists(build_dir):
            os.makedirs(build_dir)

    # Components-specific cmake args. Cmake requires all component inputs to be available
    # regardless of requested build list
    cmp_cmake_args = ""
    all_models = []
    for model, _, _, _, config_dir in complist:
        # Create the Filepath and CIME_cppdefs files
        if model == "cpl":
            config_dir = os.path.join(cimeroot, "src", "drivers", comp_interface, "cime_config")

        cmp_cmake_args += _create_build_metadata_for_component(config_dir, libroot, bldroot, case)
        all_models.append(model)

    # Call CMake
    cmake_args = get_standard_cmake_args(case, sharedpath)
    cmake_env = ""
    ninja_path = os.path.join(srcroot, "externals/ninja/bin")
    if ninja:
        cmake_args += " -GNinja "
        cmake_env += "PATH={}:$PATH ".format(ninja_path)

    if timing:
        cmake_args += " -DE3SM_ENABLE_TIMING=True "

    # Glue all pieces together:
    #  - cmake environment
    #  - common (i.e. project-wide) cmake args
    #  - component-specific cmake args
    #  - path to src folder
    cmake_cmd = "{} time cmake {} {} {}/components".format(cmake_env, cmake_args, cmp_cmake_args, srcroot)
    stat = 0
    if dry_run:
        logger.info("CMake cmd:\ncd {} && {}\n\n".format(bldroot, cmake_cmd))
    else:
        logger.info("Configuring full {} model with output to file {}".format(cime_model, bldlog))
        logger.info("   Calling cmake directly, see top of log file for specific call")
        with open(bldlog, "w") as fd:
            fd.write("Configuring with cmake cmd:\n{}\n\n".format(cmake_cmd))

        # Add logging before running
        cmake_cmd = "{} >> {} 2>&1".format(cmake_cmd, bldlog)
        stat = run_cmd(cmake_cmd, from_dir=bldroot)[0]
        expect(stat == 0, "BUILD FAIL: cmake config {} failed, cat {}".format(cime_model, bldlog))

    # Set up buildlist
    if not buildlist:
        if separate_builds:
            buildlist = all_models
        else:
            buildlist = ["cpl"]

    if "cpl" in buildlist:
        buildlist.remove("cpl")
        buildlist.append("cpl") # must come at end

    # Call Make
    logs = []
    for model in buildlist:
        make_cmd = "time {} -j {}".format(gmake if not ninja else "{} -v".format(os.path.join(ninja_path, "ninja")), gmake_j)
        if model != "cpl":
            make_cmd += " {}".format(model)
            curr_log = os.path.join(exeroot, "{}.bldlog.{}".format(model, lid))
            model_name = model
        else:
            curr_log = bldlog
            model_name = cime_model if buildlist == ["cpl"] else model

        if dry_run:
            logger.info("Build cmd:\ncd {} && {}\n\n".format(bldroot, make_cmd))
        else:
            logger.info("Building {} model with output to file {}".format(model_name, curr_log))
            logger.info("   Calling make, see top of log file for specific call")
            with open(curr_log, "a") as fd:
                fd.write("\n\nBuilding with cmd:\n{}\n\n".format(make_cmd))

            # Add logging before running
            make_cmd = "{} >> {} 2>&1".format(make_cmd, curr_log)
            stat = run_cmd(make_cmd, from_dir=bldroot)[0]
            expect(stat == 0, "BUILD FAIL: build {} failed, cat {}".format(model_name, curr_log))

        logs.append(curr_log)

    expect(not dry_run, "User requested dry-run only, terminating build")

    # Copy the just-built ${MODEL}.exe to ${MODEL}.exe.$LID
    if "cpl" in buildlist:
        safe_copy("{}/{}.exe".format(exeroot, cime_model), "{}/{}.exe.{}".format(exeroot, cime_model, lid))

    return logs

###############################################################################
def _build_checks(case, build_threaded, comp_interface,
                  debug, compiler, mpilib, complist, ninst_build, smp_value,
                  model_only, buildlist):
###############################################################################
    """
    check if a build needs to be done and warn if a clean is warrented first
    returns the relative sharedpath directory for sharedlibraries
    """
    smp_build    = case.get_value("SMP_BUILD")
    build_status = case.get_value("BUILD_STATUS")
    expect(comp_interface in ("mct", "moab", "nuopc"),
           "Only supporting mct nuopc, or moab comp_interfaces at this time, found {}".format(comp_interface))
    smpstr = ""
    ninst_value = ""
    for model, _, nthrds, ninst, _ in complist:
        if nthrds > 1:
            build_threaded = True
        if build_threaded:
            smpstr += "{}1".format(model[0])
        else:
            smpstr += "{}0".format(model[0])
        ninst_value += "{}{:d}".format((model[0]),ninst)

    case.set_value("SMP_VALUE", smpstr)
    case.set_value("NINST_VALUE", ninst_value)

    debugdir = "debug" if debug else "nodebug"
    threaddir = "threads" if build_threaded else "nothreads"
    sharedpath = os.path.join(compiler, mpilib, debugdir, threaddir, comp_interface)

    logger.debug("compiler={} mpilib={} debugdir={} threaddir={}"
                 .format(compiler,mpilib,debugdir,threaddir))

    expect(ninst_build == ninst_value or ninst_build == "0",
            """
ERROR, NINST VALUES HAVE CHANGED
  NINST_BUILD = {}
  NINST_VALUE = {}
  A manual clean of your obj directories is strongly recommended
  You should execute the following:
    ./case.build --clean
  Then rerun the build script interactively
  ---- OR ----
  You can override this error message at your own risk by executing:
    ./xmlchange -file env_build.xml -id NINST_BUILD -val 0
  Then rerun the build script interactively
""".format(ninst_build, ninst_value))

    expect(smp_build == smpstr or smp_build == "0",
            """
ERROR, SMP VALUES HAVE CHANGED
  SMP_BUILD = {}
  SMP_VALUE = {}
  smpstr = {}
  A manual clean of your obj directories is strongly recommended
  You should execute the following:
    ./case.build --clean
  Then rerun the build script interactively
  ---- OR ----
  You can override this error message at your own risk by executing:
    ./xmlchange -file env_build.xml -id SMP_BUILD -val 0
  Then rerun the build script interactively
""".format(smp_build, smp_value, smpstr))

    expect(build_status == 0,
           """
ERROR env_build HAS CHANGED
  A manual clean of your obj directories is required
  You should execute the following:
    ./case.build --clean-all
""")

    case.set_value("BUILD_COMPLETE", False)

    # User may have rm -rf their build directory
    case.create_dirs()

    case.flush()
    if not model_only and not buildlist:
        logger.info("Generating component namelists as part of build")
        case.create_namelists()

    return sharedpath

###############################################################################
def _build_libraries(case, exeroot, sharedpath, caseroot, cimeroot, libroot, lid, compiler, buildlist, comp_interface, complist):
###############################################################################

    shared_lib = os.path.join(exeroot, sharedpath, "lib")
    shared_inc = os.path.join(exeroot, sharedpath, "include")
    for shared_item in [shared_lib, shared_inc]:
        if (not os.path.exists(shared_item)):
            os.makedirs(shared_item)

    mpilib = case.get_value("MPILIB")
    ufs_driver = os.environ.get("UFS_DRIVER")
    cpl_in_complist = False
    for l in complist:
        if "cpl" in l:
            cpl_in_complist = True
    if ufs_driver:
        logger.info("UFS_DRIVER is set to {}".format(ufs_driver))
    if ufs_driver and ufs_driver == 'nems' and not cpl_in_complist:
        libs = []
    else:
        libs = ["gptl", "mct", "pio", "csm_share"]

    if mpilib == "mpi-serial":
        libs.insert(0, mpilib)

    if uses_kokkos(case):
        libs.append("kokkos")

    # Build shared code of CDEPS nuopc data models
    build_script = {}
    if comp_interface == "nuopc" and (not ufs_driver or ufs_driver != 'nems'):
        libs.append("CDEPS")

    ocn_model = case.get_value("COMP_OCN")
    atm_model = case.get_value("COMP_ATM")
    if ocn_model == 'mom' or atm_model == "fv3gfs":
        libs.append("FMS")

    files = Files(comp_interface=comp_interface)
    for lib in libs:
        build_script[lib] = files.get_value("BUILD_LIB_FILE",{"lib":lib})


    sharedlibroot = os.path.abspath(case.get_value("SHAREDLIBROOT"))
    # Check if we need to build our own cprnc
    if case.get_value("TEST"):
        cprnc_loc = case.get_value("CCSM_CPRNC")
        full_lib_path = os.path.join(sharedlibroot, compiler, "cprnc")
        if not cprnc_loc or not os.path.exists(cprnc_loc):
            case.set_value("CCSM_CPRNC", os.path.join(full_lib_path, "cprnc"))
            if not os.path.isdir(full_lib_path):
                os.makedirs(full_lib_path)
                libs.insert(0,"cprnc")

    logs = []

    for lib in libs:
        if buildlist is not None and lib not in buildlist:
            continue

        if lib == "csm_share":
            # csm_share adds its own dir name
            full_lib_path = os.path.join(sharedlibroot, sharedpath)
        elif lib == "mpi-serial":
            full_lib_path = os.path.join(sharedlibroot, sharedpath, "mct", lib)
        elif lib == "cprnc":
            full_lib_path = os.path.join(sharedlibroot, compiler, "cprnc")
        else:
            full_lib_path = os.path.join(sharedlibroot, sharedpath, lib)

        # pio build creates its own directory
        if (lib != "pio" and not os.path.isdir(full_lib_path)):
            os.makedirs(full_lib_path)

        file_build = os.path.join(exeroot, "{}.bldlog.{}".format(lib, lid))
        if lib in build_script.keys():
            my_file = build_script[lib]
        else:
            my_file = os.path.join(cimeroot, "src", "build_scripts", "buildlib.{}".format(lib))
        expect(os.path.exists(my_file),"Build script {} for component {} not found.".format(my_file, lib))
        logger.info("Building {} with output to file {}".format(lib,file_build))

        run_sub_or_cmd(my_file, [full_lib_path, os.path.join(exeroot, sharedpath), caseroot], 'buildlib',
                       [full_lib_path, os.path.join(exeroot, sharedpath), case], logfile=file_build)

        analyze_build_log(lib, file_build, compiler)
        logs.append(file_build)
        if lib == "pio":
            bldlog = open(file_build, "r")
            for line in bldlog:
                if re.search("Current setting for", line):
                    logger.warning(line)

    # clm not a shared lib for E3SM
    if get_model() != "e3sm" and (buildlist is None or "lnd" in buildlist):
        comp_lnd = case.get_value("COMP_LND")
        if comp_lnd == "clm":
            logging.info("         - Building clm library ")
            esmfdir = "esmf" if case.get_value("USE_ESMF_LIB") else "noesmf"
            bldroot = os.path.join(sharedlibroot, sharedpath, comp_interface, esmfdir, "clm","obj" )
            libroot = os.path.join(exeroot, sharedpath, comp_interface, esmfdir, "lib")
            incroot = os.path.join(exeroot, sharedpath, comp_interface, esmfdir, "include")
            file_build = os.path.join(exeroot, "lnd.bldlog.{}".format( lid))
            config_lnd_dir = os.path.dirname(case.get_value("CONFIG_LND_FILE"))

            for ndir in [bldroot, libroot, incroot]:
                if (not os.path.isdir(ndir)):
                    os.makedirs(ndir)

            smp = "SMP" in os.environ and os.environ["SMP"] == "TRUE"
            # thread_bad_results captures error output from thread (expected to be empty)
            # logs is a list of log files to be compressed and added to the case logs/bld directory
            thread_bad_results = []
            _build_model_thread(config_lnd_dir, "lnd", comp_lnd, caseroot, libroot, bldroot, incroot,
                                file_build, thread_bad_results, smp, compiler)
            logs.append(file_build)
            expect(not thread_bad_results, "\n".join(thread_bad_results))

    case.flush() # python sharedlib subs may have made XML modifications
    return logs

###############################################################################
def _build_model_thread(config_dir, compclass, compname, caseroot, libroot, bldroot, incroot, file_build,
                        thread_bad_results, smp, compiler):
###############################################################################
    logger.info("Building {} with output to {}".format(compclass, file_build))
    t1 = time.time()
    cmd = os.path.join(caseroot, "SourceMods", "src." + compname, "buildlib")
    if os.path.isfile(cmd):
        logger.warning("WARNING: using local buildlib script for {}".format(compname))
    else:
        cmd = os.path.join(config_dir, "buildlib")
        expect(os.path.isfile(cmd), "Could not find buildlib for {}".format(compname))

    compile_cmd = "MODEL={compclass} COMP_CLASS={compclass} COMP_NAME={compname} {cmd} {caseroot} {libroot} {bldroot} ".\
        format(compclass=compclass, compname=compname, cmd=cmd, caseroot=caseroot, libroot=libroot, bldroot=bldroot)
    if get_model() != "ufs":
        compile_cmd = "SMP={} {}".format(stringify_bool(smp), compile_cmd)

    if check_for_python(cmd):
        logging_options = get_logging_options()
        if logging_options != "":
            compile_cmd = compile_cmd + logging_options

    with open(file_build, "w") as fd:
        stat = run_cmd(compile_cmd,
                       from_dir=bldroot,  arg_stdout=fd,
                       arg_stderr=subprocess.STDOUT)[0]

    if stat != 0:
        thread_bad_results.append("BUILD FAIL: {}.buildlib failed, cat {}".format(compname, file_build))

    analyze_build_log(compclass, file_build, compiler)

    for mod_file in glob.glob(os.path.join(bldroot, "*_[Cc][Oo][Mm][Pp]_*.mod")):
        safe_copy(mod_file, incroot)

    t2 = time.time()
    logger.info("{} built in {:f} seconds".format(compname, (t2 - t1)))

###############################################################################
def _create_build_metadata_for_component(config_dir, libroot, bldroot, case):
###############################################################################
    """
    Ensure that crucial Filepath and CIME_CPPDEFS files exist for this component.
    In many cases, the bld/configure script will have already created these.
    """
    bc_path = os.path.join(config_dir, "buildlib_cmake")
    expect(os.path.exists(bc_path), "Missing: {}".format(bc_path))
    buildlib = imp.load_source("buildlib_cmake", os.path.join(config_dir, "buildlib_cmake"))
    cmake_args = buildlib.buildlib(bldroot, libroot, case)
    return "" if cmake_args is None else cmake_args

###############################################################################
def _clean_impl(case, cleanlist, clean_all, clean_depends):
###############################################################################
    exeroot = os.path.abspath(case.get_value("EXEROOT"))
    case.load_env()
    if clean_all:
        # If cleanlist is empty just remove the bld directory
        expect(exeroot is not None,"No EXEROOT defined in case")
        if os.path.isdir(exeroot):
            logging.info("cleaning directory {}".format(exeroot))
            shutil.rmtree(exeroot)

        # if clean_all is True also remove the sharedlibpath
        sharedlibroot = os.path.abspath(case.get_value("SHAREDLIBROOT"))
        expect(sharedlibroot is not None,"No SHAREDLIBROOT defined in case")
        if sharedlibroot != exeroot and os.path.isdir(sharedlibroot):
            logging.warning("cleaning directory {}".format(sharedlibroot))
            shutil.rmtree(sharedlibroot)

    else:
        expect((cleanlist is not None and len(cleanlist) > 0) or
               (clean_depends is not None and len(clean_depends)),"Empty cleanlist not expected")
        gmake = case.get_value("GMAKE")

        cleanlist = [] if cleanlist is None else cleanlist
        clean_depends = [] if clean_depends is None else clean_depends
        things_to_clean = cleanlist + clean_depends

        cmake_comp_root = os.path.join(exeroot, "cmake-bld", "cmake")
        casetools = case.get_value("CASETOOLS")
        classic_cmd = "{} -f {} {}".format(gmake, os.path.join(casetools, "Makefile"),
                                           get_standard_makefile_args(case, shared_lib=True))

        for clean_item in things_to_clean:
            logging.info("Cleaning {}".format(clean_item))
            cmake_path = os.path.join(cmake_comp_root, clean_item)
            if os.path.exists(cmake_path):
                # Item was created by cmake build system
                clean_cmd = "cd {} && {} clean".format(cmake_path, gmake)
            else:
                # Item was created by classic build system
                clean_cmd = "{} {}{}".format(classic_cmd, "clean" if clean_item in cleanlist else "clean_depends", clean_item)

            logger.info("calling {}".format(clean_cmd))
            run_cmd_no_fail(clean_cmd)

    # unlink Locked files directory
    unlock_file("env_build.xml")

    # reset following values in xml files
    case.set_value("SMP_BUILD",str(0))
    case.set_value("NINST_BUILD",str(0))
    case.set_value("BUILD_STATUS",str(0))
    case.set_value("BUILD_COMPLETE","FALSE")
    case.flush()

###############################################################################
def _case_build_impl(caseroot, case, sharedlib_only, model_only, buildlist,
                     save_build_provenance, separate_builds, ninja, dry_run, timing):
###############################################################################

    t1 = time.time()

    expect(not (sharedlib_only and model_only),
           "Contradiction: both sharedlib_only and model_only")
    expect(not (dry_run and not model_only),
           "Dry-run is only for model builds, please build sharedlibs first")
    logger.info("Building case in directory {}".format(caseroot))
    logger.info("sharedlib_only is {}".format(sharedlib_only))
    logger.info("model_only is {}".format(model_only))

    expect(os.path.isdir(caseroot), "'{}' is not a valid directory".format(caseroot))
    os.chdir(caseroot)

    expect(os.path.exists(get_batch_script_for_job(case.get_primary_job())),
           "ERROR: must invoke case.setup script before calling build script ")

    cimeroot = case.get_value("CIMEROOT")

    comp_classes = case.get_values("COMP_CLASSES")

    case.check_lockedfiles(skip="env_batch")

    # Retrieve relevant case data
    # This environment variable gets set for cesm Make and
    # needs to be unset before building again.
    if "MODEL" in os.environ:
        del os.environ["MODEL"]
    build_threaded      = case.get_build_threaded()
    exeroot             = os.path.abspath(case.get_value("EXEROOT"))
    incroot             = os.path.abspath(case.get_value("INCROOT"))
    libroot             = os.path.abspath(case.get_value("LIBROOT"))
    multi_driver = case.get_value("MULTI_DRIVER")
    complist = []
    ninst = 1
    comp_interface      = case.get_value("COMP_INTERFACE")
    for comp_class in comp_classes:
        if comp_class == "CPL":
            config_dir = None
            if multi_driver:
                ninst = case.get_value("NINST_MAX")
        else:
            config_dir = os.path.dirname(case.get_value("CONFIG_{}_FILE".format(comp_class)))
            if multi_driver:
                ninst = 1
            else:
                ninst = case.get_value("NINST_{}".format(comp_class))

        comp = case.get_value("COMP_{}".format(comp_class))
        if comp_interface == 'nuopc' and comp in ('satm', 'slnd', 'sesp', 'sglc', 'srof', 'sice', 'socn', 'swav', 'siac'):
            continue
        thrds =  case.get_value("NTHRDS_{}".format(comp_class))
        expect(ninst is not None,"Failed to get ninst for comp_class {}".format(comp_class))
        complist.append((comp_class.lower(), comp, thrds, ninst, config_dir ))
        os.environ["COMP_{}".format(comp_class)] = comp

    compiler            = case.get_value("COMPILER")
    mpilib              = case.get_value("MPILIB")
    debug               = case.get_value("DEBUG")
    ninst_build         = case.get_value("NINST_BUILD")
    smp_value           = case.get_value("SMP_VALUE")
    clm_use_petsc       = case.get_value("CLM_USE_PETSC")
    cism_use_trilinos   = case.get_value("CISM_USE_TRILINOS")
    mali_use_albany     = case.get_value("MALI_USE_ALBANY")
    mach                = case.get_value("MACH")

    # Load some params into env
    os.environ["BUILD_THREADED"]       = stringify_bool(build_threaded)
    cime_model = get_model()

    if cime_model == "e3sm" and mach == "titan" and compiler == "pgiacc":
        case.set_value("CAM_TARGET", "preqx_acc")

    # This is a timestamp for the build , not the same as the testid,
    # and this case may not be a test anyway. For a production
    # experiment there may be many builds of the same case.
    lid               = get_timestamp("%y%m%d-%H%M%S")
    os.environ["LID"] = lid

    # Set the overall USE_PETSC variable to TRUE if any of the
    # *_USE_PETSC variables are TRUE.
    # For now, there is just the one CLM_USE_PETSC variable, but in
    # the future there may be others -- so USE_PETSC will be true if
    # ANY of those are true.

    use_petsc = clm_use_petsc
    case.set_value("USE_PETSC", use_petsc)

    # Set the overall USE_TRILINOS variable to TRUE if any of the
    # *_USE_TRILINOS variables are TRUE.
    # For now, there is just the one CISM_USE_TRILINOS variable, but in
    # the future there may be others -- so USE_TRILINOS will be true if
    # ANY of those are true.

    use_trilinos = False if cism_use_trilinos is None else cism_use_trilinos
    case.set_value("USE_TRILINOS", use_trilinos)

    # Set the overall USE_ALBANY variable to TRUE if any of the
    # *_USE_ALBANY variables are TRUE.
    # For now, there is just the one MALI_USE_ALBANY variable, but in
    # the future there may be others -- so USE_ALBANY will be true if
    # ANY of those are true.

    use_albany = stringify_bool(mali_use_albany)
    case.set_value("USE_ALBANY", use_albany)

    # Load modules
    case.load_env()

    sharedpath = _build_checks(case, build_threaded, comp_interface,
                               debug, compiler, mpilib, complist, ninst_build, smp_value,
                               model_only, buildlist)

    t2 = time.time()
    logs = []

    if not model_only:
        logs = _build_libraries(case, exeroot, sharedpath, caseroot,
                                cimeroot, libroot, lid, compiler, buildlist, comp_interface, complist)

    if not sharedlib_only:
        if get_model() == "e3sm":
            logs.extend(_build_model_cmake(exeroot, complist, lid, cimeroot, buildlist,
                                           comp_interface, sharedpath, separate_builds, ninja, dry_run, timing, case))
        else:
            os.environ["INSTALL_SHAREDPATH"] = os.path.join(exeroot, sharedpath) # for MPAS makefile generators
            logs.extend(_build_model(build_threaded, exeroot, incroot, complist,
                                     lid, caseroot, cimeroot, compiler, buildlist, comp_interface))

        if not buildlist:
            # in case component build scripts updated the xml files, update the case object
            case.read_xml()
            # Note, doing buildlists will never result in the system thinking the build is complete

    post_build(case, logs, build_complete=not (buildlist or sharedlib_only),
               save_build_provenance=save_build_provenance)

    t3 = time.time()

    if not sharedlib_only:
        logger.info("Time spent not building: {:f} sec".format(t2 - t1))
        logger.info("Time spent building: {:f} sec".format(t3 - t2))
        logger.info("MODEL BUILD HAS FINISHED SUCCESSFULLY")

    return True

###############################################################################
def post_build(case, logs, build_complete=False, save_build_provenance=True):
###############################################################################
    for log in logs:
        gzip_existing_file(log)

    if build_complete:
        # must ensure there's an lid
        lid = os.environ["LID"] if "LID" in os.environ else get_timestamp("%y%m%d-%H%M%S")
        if save_build_provenance:
            save_build_provenance_sub(case, lid=lid)
        # Set XML to indicate build complete
        case.set_value("BUILD_COMPLETE", True)
        case.set_value("BUILD_STATUS", 0)
        if "SMP_VALUE" in os.environ:
            case.set_value("SMP_BUILD", os.environ["SMP_VALUE"])

        case.flush()

        lock_file("env_build.xml", caseroot=case.get_value("CASEROOT"))

###############################################################################
def case_build(caseroot, case, sharedlib_only=False, model_only=False, buildlist=None, save_build_provenance=True, separate_builds=False, ninja=False, dry_run=False, timing=False):
###############################################################################
    functor = lambda: _case_build_impl(caseroot, case, sharedlib_only, model_only, buildlist,
                                       save_build_provenance, separate_builds, ninja, dry_run, timing)
    cb = "case.build"
    if (sharedlib_only == True):
        cb = cb + " (SHAREDLIB_BUILD)"
    if (model_only == True):
        cb = cb + " (MODEL_BUILD)"
    return run_and_log_case_status(functor, cb, caseroot=caseroot)

###############################################################################
def clean(case, cleanlist=None, clean_all=False, clean_depends=None):
###############################################################################
    functor = lambda: _clean_impl(case, cleanlist, clean_all, clean_depends)
    return run_and_log_case_status(functor, "build.clean", caseroot=case.get_value("CASEROOT"))
