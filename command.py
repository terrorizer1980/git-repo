# Copyright (C) 2008 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import multiprocessing
import os
import optparse
import platform
import re
import sys

from event_log import EventLog
from error import NoSuchProjectError
from error import InvalidProjectGroupsError
import progress


# Number of projects to submit to a single worker process at a time.
# This number represents a tradeoff between the overhead of IPC and finer
# grained opportunity for parallelism. This particular value was chosen by
# iterating through powers of two until the overall performance no longer
# improved. The performance of this batch size is not a function of the
# number of cores on the system.
WORKER_BATCH_SIZE = 32


# How many jobs to run in parallel by default?  This assumes the jobs are
# largely I/O bound and do not hit the network.
DEFAULT_LOCAL_JOBS = min(os.cpu_count(), 8)


class Command(object):
  """Base class for any command line action in repo.
  """

  common = False
  event_log = EventLog()
  manifest = None
  _optparse = None

  # Whether this command supports running in parallel.  If greater than 0,
  # it is the number of parallel jobs to default to.
  PARALLEL_JOBS = None

  def WantPager(self, _opt):
    return False

  def ReadEnvironmentOptions(self, opts):
    """ Set options from environment variables. """

    env_options = self._RegisteredEnvironmentOptions()

    for env_key, opt_key in env_options.items():
      # Get the user-set option value if any
      opt_value = getattr(opts, opt_key)

      # If the value is set, it means the user has passed it as a command
      # line option, and we should use that.  Otherwise we can try to set it
      # with the value from the corresponding environment variable.
      if opt_value is not None:
        continue

      env_value = os.environ.get(env_key)
      if env_value is not None:
        setattr(opts, opt_key, env_value)

    return opts

  @property
  def OptionParser(self):
    if self._optparse is None:
      try:
        me = 'repo %s' % self.NAME
        usage = self.helpUsage.strip().replace('%prog', me)
      except AttributeError:
        usage = 'repo %s' % self.NAME
      epilog = 'Run `repo help %s` to view the detailed manual.' % self.NAME
      self._optparse = optparse.OptionParser(usage=usage, epilog=epilog)
      self._CommonOptions(self._optparse)
      self._Options(self._optparse)
    return self._optparse

  def _CommonOptions(self, p, opt_v=True):
    """Initialize the option parser with common options.

    These will show up for *all* subcommands, so use sparingly.
    NB: Keep in sync with repo:InitParser().
    """
    g = p.add_option_group('Logging options')
    opts = ['-v'] if opt_v else []
    g.add_option(*opts, '--verbose',
                 dest='output_mode', action='store_true',
                 help='show all output')
    g.add_option('-q', '--quiet',
                 dest='output_mode', action='store_false',
                 help='only show errors')

    if self.PARALLEL_JOBS is not None:
      p.add_option(
          '-j', '--jobs',
          type=int, default=self.PARALLEL_JOBS,
          help='number of jobs to run in parallel (default: %s)' % self.PARALLEL_JOBS)

  def _Options(self, p):
    """Initialize the option parser with subcommand-specific options."""

  def _RegisteredEnvironmentOptions(self):
    """Get options that can be set from environment variables.

    Return a dictionary mapping environment variable name
    to option key name that it can override.

    Example: {'REPO_MY_OPTION': 'my_option'}

    Will allow the option with key value 'my_option' to be set
    from the value in the environment variable named 'REPO_MY_OPTION'.

    Note: This does not work properly for options that are explicitly
    set to None by the user, or options that are defined with a
    default value other than None.

    """
    return {}

  def Usage(self):
    """Display usage and terminate.
    """
    self.OptionParser.print_usage()
    sys.exit(1)

  def CommonValidateOptions(self, opt, args):
    """Validate common options."""
    opt.quiet = opt.output_mode is False
    opt.verbose = opt.output_mode is True

  def ValidateOptions(self, opt, args):
    """Validate the user options & arguments before executing.

    This is meant to help break the code up into logical steps.  Some tips:
    * Use self.OptionParser.error to display CLI related errors.
    * Adjust opt member defaults as makes sense.
    * Adjust the args list, but do so inplace so the caller sees updates.
    * Try to avoid updating self state.  Leave that to Execute.
    """

  def Execute(self, opt, args):
    """Perform the action, after option parsing is complete.
    """
    raise NotImplementedError

  @staticmethod
  def ExecuteInParallel(jobs, func, inputs, callback, output=None, ordered=False):
    """Helper for managing parallel execution boiler plate.

    For subcommands that can easily split their work up.

    Args:
      jobs: How many parallel processes to use.
      func: The function to apply to each of the |inputs|.  Usually a
          functools.partial for wrapping additional arguments.  It will be run
          in a separate process, so it must be pickalable, so nested functions
          won't work.  Methods on the subcommand Command class should work.
      inputs: The list of items to process.  Must be a list.
      callback: The function to pass the results to for processing.  It will be
          executed in the main thread and process the results of |func| as they
          become available.  Thus it may be a local nested function.  Its return
          value is passed back directly.  It takes three arguments:
          - The processing pool (or None with one job).
          - The |output| argument.
          - An iterator for the results.
      output: An output manager.  May be progress.Progess or color.Coloring.
      ordered: Whether the jobs should be processed in order.

    Returns:
      The |callback| function's results are returned.
    """
    try:
      # NB: Multiprocessing is heavy, so don't spin it up for one job.
      if len(inputs) == 1 or jobs == 1:
        return callback(None, output, (func(x) for x in inputs))
      else:
        with multiprocessing.Pool(jobs) as pool:
          submit = pool.imap if ordered else pool.imap_unordered
          return callback(pool, output, submit(func, inputs, chunksize=WORKER_BATCH_SIZE))
    finally:
      if isinstance(output, progress.Progress):
        output.end()

  def _ResetPathToProjectMap(self, projects):
    self._by_path = dict((p.worktree, p) for p in projects)

  def _UpdatePathToProjectMap(self, project):
    self._by_path[project.worktree] = project

  def _GetProjectByPath(self, manifest, path):
    project = None
    if os.path.exists(path):
      oldpath = None
      while (path and
             path != oldpath and
             path != manifest.topdir):
        try:
          project = self._by_path[path]
          break
        except KeyError:
          oldpath = path
          path = os.path.dirname(path)
      if not project and path == manifest.topdir:
        try:
          project = self._by_path[path]
        except KeyError:
          pass
    else:
      try:
        project = self._by_path[path]
      except KeyError:
        pass
    return project

  def GetProjects(self, args, manifest=None, groups='', missing_ok=False,
                  submodules_ok=False):
    """A list of projects that match the arguments.
    """
    if not manifest:
      manifest = self.manifest
    all_projects_list = manifest.projects
    result = []

    mp = manifest.manifestProject

    if not groups:
      groups = manifest.GetGroupsStr()
    groups = [x for x in re.split(r'[,\s]+', groups) if x]

    if not args:
      derived_projects = {}
      for project in all_projects_list:
        if submodules_ok or project.sync_s:
          derived_projects.update((p.name, p)
                                  for p in project.GetDerivedSubprojects())
      all_projects_list.extend(derived_projects.values())
      for project in all_projects_list:
        if (missing_ok or project.Exists) and project.MatchesGroups(groups):
          result.append(project)
    else:
      self._ResetPathToProjectMap(all_projects_list)

      for arg in args:
        # We have to filter by manifest groups in case the requested project is
        # checked out multiple times or differently based on them.
        projects = [project for project in manifest.GetProjectsWithName(arg)
                    if project.MatchesGroups(groups)]

        if not projects:
          path = os.path.abspath(arg).replace('\\', '/')
          project = self._GetProjectByPath(manifest, path)

          # If it's not a derived project, update path->project mapping and
          # search again, as arg might actually point to a derived subproject.
          if (project and not project.Derived and (submodules_ok or
                                                   project.sync_s)):
            search_again = False
            for subproject in project.GetDerivedSubprojects():
              self._UpdatePathToProjectMap(subproject)
              search_again = True
            if search_again:
              project = self._GetProjectByPath(manifest, path) or project

          if project:
            projects = [project]

        if not projects:
          raise NoSuchProjectError(arg)

        for project in projects:
          if not missing_ok and not project.Exists:
            raise NoSuchProjectError('%s (%s)' % (arg, project.relpath))
          if not project.MatchesGroups(groups):
            raise InvalidProjectGroupsError(arg)

        result.extend(projects)

    def _getpath(x):
      return x.relpath
    result.sort(key=_getpath)
    return result

  def FindProjects(self, args, inverse=False):
    result = []
    patterns = [re.compile(r'%s' % a, re.IGNORECASE) for a in args]
    for project in self.GetProjects(''):
      for pattern in patterns:
        match = pattern.search(project.name) or pattern.search(project.relpath)
        if not inverse and match:
          result.append(project)
          break
        if inverse and match:
          break
      else:
        if inverse:
          result.append(project)
    result.sort(key=lambda project: project.relpath)
    return result


class InteractiveCommand(Command):
  """Command which requires user interaction on the tty and
     must not run within a pager, even if the user asks to.
  """

  def WantPager(self, _opt):
    return False


class PagedCommand(Command):
  """Command which defaults to output in a pager, as its
     display tends to be larger than one screen full.
  """

  def WantPager(self, _opt):
    return True


class MirrorSafeCommand(object):
  """Command permits itself to run within a mirror,
     and does not require a working directory.
  """


class GitcAvailableCommand(object):
  """Command that requires GITC to be available, but does
     not require the local client to be a GITC client.
  """


class GitcClientCommand(object):
  """Command that requires the local client to be a GITC
     client.
  """
