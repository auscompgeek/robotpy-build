import inspect
import os
from os.path import abspath, dirname, join, normpath, relpath, sep
import shutil
import yaml

from header2whatever.config import Config
from header2whatever.parse import process_config

from setuptools import Extension

from .hooks_datacfg import HooksDataYaml
from .download import download_and_extract_zip


class Wrapper:
    """
        Wraps downloading bindings and generating them
    """

    def __init__(self, name, wrapcfg, setup):

        # must match PkgCfg.name
        self.name = wrapcfg.name
        # must match PkgCfg.import_name
        self.import_name = name

        self.setup_root = setup.root
        self.root = join(setup.root, *self.import_name.split("."))
        self.cfg = wrapcfg
        self.platform = setup.platform
        self.pkgcfg = setup.pkgcfg

        if not self.cfg.artname:
            self.cfg.artname = self.cfg.name

        self.extension = None
        if self.cfg.sources or self.cfg.generate:
            # extensions just hold data about what to actually build, we can
            # actually modify extensions all the way up until the build
            # really happens
            extname = f"{self.import_name}.{self.name}"
            self.extension = Extension(extname, self.cfg.sources, language="c++")

        if self.cfg.generate and not self.cfg.generation_data:
            raise ValueError(
                "generation_data must be specified when generate is specified"
            )

        # Setup an entry point (written during build_clib)
        entry_point = f"{self.cfg.name} = {name}.pkgcfg"

        setup_kwargs = setup.setup_kwargs
        ep = setup_kwargs.setdefault("entry_points", {})
        ep.setdefault("robotpybuild", []).append(entry_point)

    def _dl_url(self, thing):
        # TODO: support development against locally installed things?
        base = self.cfg.baseurl
        art = self.cfg.artname
        ver = self.cfg.version
        return f"{base}/{art}/{ver}/{art}-{ver}-{thing}.zip"

    def _extract_zip_to(self, thing, dst, cache):
        download_and_extract_zip(self._dl_url(thing), to=dst, cache=cache)

    # pkgcfg interface
    def get_include_dirs(self):
        return [join(self.root, "include")]

    def get_library_dirs(self):
        return [join(self.root, "lib")]

    def get_library_names(self):
        return [self.cfg.name]

    def _all_includes(self, include_rpyb):
        includes = self.get_include_dirs()
        for dep in self.cfg.depends:
            includes.extend(self.pkgcfg.get_pkg(dep).get_include_dirs())
        if include_rpyb:
            includes.extend(self.pkgcfg.get_pkg("robotpy-build").get_include_dirs())
        return includes

    def _all_library_dirs(self):
        libs = self.get_library_dirs()
        for dep in self.cfg.depends:
            libs.extend(self.pkgcfg.get_pkg(dep).get_library_dirs())
        return libs

    def _all_library_names(self):
        libs = self.get_library_names()
        for dep in self.cfg.depends:
            libs.extend(self.pkgcfg.get_pkg(dep).get_library_names())
        return list(reversed(libs))

    def on_build_dl(self, cache):

        libdir = join(self.root, "lib")
        incdir = join(self.root, "include")
        initpy = join(self.root, "__init__.py")
        pkgcfgpy = join(self.root, "pkgcfg.py")

        # Remove downloaded/generated artifacts first
        shutil.rmtree(libdir, ignore_errors=True)
        shutil.rmtree(incdir, ignore_errors=True)

        try:
            os.unlink(initpy)
        except OSError:
            pass
        try:
            os.unlink(pkgcfgpy)
        except OSError:
            pass

        self._extract_zip_to("headers", incdir, cache)

        libnames = self.cfg.libs
        if not libnames:
            libnames = [self.cfg.name]

        libext = self.cfg.libexts.get(self.platform.libext, self.platform.libext)

        libnames = [f"{self.platform.libprefix}{lib}{libext}" for lib in libnames]

        os.makedirs(libdir)
        to = {
            join(self.platform.os, self.platform.arch, "shared", libname): join(
                libdir, libname
            )
            for libname in libnames
        }

        self._extract_zip_to(f"{self.platform.os}{self.platform.arch}", to, cache)

        self._write_init_py(initpy, libnames)
        self._write_pkgcfg_py(pkgcfgpy)

    def _write_init_py(self, fname, libnames):
        init = inspect.cleandoc(
            """

        # fmt: off
        # This file is automatically generated, DO NOT EDIT

        from os.path import abspath, join, dirname
        _root = abspath(dirname(__file__))

        ##IMPORTS##

        from ctypes import cdll

        """
        )

        init += "\n"

        for libname in libnames:
            init += f'_lib = cdll.LoadLibrary(join(_root, "lib", "{libname}"))\n'

        imports = []
        for dep in self.cfg.depends:
            pkg = self.pkgcfg.get_pkg(dep)
            if pkg.import_name:
                imports.append(pkg.import_name)

        if imports:
            imports = "# runtime dependencies\nimport " + "\nimport ".join(imports)
        else:
            imports = ""

        init = init.replace("##IMPORTS##", imports)

        with open(join(self.root, "__init__.py"), "w") as fp:
            fp.write(init)

    def _write_pkgcfg_py(self, fname):

        # write pkgcfg.py
        pkgcfg = inspect.cleandoc(
            f"""
        # fmt: off
        # This file is automatically generated, DO NOT EDIT

        from os.path import abspath, join, dirname
        _root = abspath(dirname(__file__))

        import_name = "{self.import_name}"

        def get_include_dirs():
            return [join(_root, "include")##EXTRAINCLUDES##]

        def get_library_dirs():
            return [join(_root, "lib")]
        
        def get_library_names():
            return ["{self.cfg.name}"]
        """
        )

        extraincludes = ""
        if self.cfg.extra_headers:
            # these are relative to the root of the project, need
            # to resolve the path relative to the pkgcfg directory
            pth = join(*self.import_name.split("."))

            for h in self.cfg.extra_headers:
                h = '", "'.join(relpath(normpath(h), pth).split(sep))
                extraincludes += f', join(_root, "{h}")'

        pkgcfg = pkgcfg.replace("##EXTRAINCLUDES##", extraincludes)

        with open(fname, "w") as fp:
            fp.write(pkgcfg)

    def on_build_gen(self):

        if not self.cfg.generate:
            return

        thisdir = abspath(dirname(__file__))

        incdir = join(self.root, "include")
        outdir = join(self.root, "gensrc")
        hooks = join(thisdir, "hooks.py")
        cpp_tmpl = join(thisdir, "templates", "gen_pybind11.cpp.j2")

        pp_includes = self._all_includes(False)

        shutil.rmtree(outdir, ignore_errors=True)
        os.makedirs(outdir)

        data = {}
        if self.cfg.generation_data:
            datafile = join(self.setup_root, normpath(self.cfg.generation_data))

            with open(datafile) as fp:
                data = yaml.safe_load(fp)

        data = HooksDataYaml(data)
        data.validate()

        sources = self.cfg.sources[:]

        for gen in self.cfg.generate:
            for name, header in gen.items():

                dst = join(outdir, f"{name}.cpp")
                sources.append(dst)

                # for each thing, create a h2w configuration dictionary
                cfgd = {
                    "headers": [join(incdir, normpath(header))],
                    "templates": [{"src": cpp_tmpl, "dst": dst}],
                    "hooks": hooks,
                    "preprocess": True,
                    "pp_include_paths": pp_includes,
                    "vars": {"mod_fn": name},
                }

                cfg = Config(cfgd)
                cfg.validate()
                cfg.root = incdir

                process_config(cfg, data)

        # generate an inline file that can be included + called
        self._write_module_inl(outdir)

        # update the build extension so that build_ext works
        self.extension.sources = sources
        self.extension.include_dirs = self._all_includes(True)
        self.extension.library_dirs = self._all_library_dirs()
        self.extension.libraries = self._all_library_names()

    def _write_module_inl(self, outdir):

        decls = []
        calls = []

        for gen in self.cfg.generate:
            for name, header in gen.items():
                decls.append(f"void init_{name}(py::module &m);")
                calls.append(f"    init_{name}(m);")

        content = (
            inspect.cleandoc(
                """

        // This file is autogenerated, DO NOT EDIT
        #include <pybind11/pybind11.h>
        namespace py = pybind11;

        // forward declarations
        ##DECLS##

        static void initWrapper(py::module &m) {
        ##CALLS##
        }
        
        """
            )
            .replace("##DECLS##", "\n".join(decls))
            .replace("##CALLS##", "\n".join(calls))
        )

        with open(join(outdir, "module.hpp"), "w") as fp:
            fp.write(content)
