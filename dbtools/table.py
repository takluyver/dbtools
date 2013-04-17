import numpy as np
import pandas as pd
import re
import os

from .util import sql_execute, dict_to_dtypes


class Table(object):
    """A frame-like interface to a SQLite database table.

    """

    @classmethod
    def exists(cls, db, name, verbose=False):
        """Check if a table called `name` exists in the database `db`.

        Parameters
        ----------
        db : string
            Path to the SQLite database.

        name : string
            Name of the desired table.

        verbose : bool (default=False)
            Print out SQL command information.


        Returns
        -------
        True if the table exists, False otherwise

        """

        # if the database doesn't exist, neither does the table
        if not os.path.exists(db):
            return False

        # select the names of all tables in the database
        cmd = "SELECT name FROM sqlite_master WHERE type='table'"
        result = sql_execute(db, cmd, fetchall=True, verbose=verbose)

        # try to match `name` to one of the table names
        for table in result:
            if name == table[0]:
                return True
        return False

    @classmethod
    def create(cls, db, name, dtypes, primary_key=None,
               autoincrement=False, verbose=False):
        """Create a table called `name` in the database `db`.

        Parameters
        ----------
        db : string
            Path to the SQLite database.

        name : string
            Name of the desired table.

        dtypes : list of 2-tuples
            Each tuple corresponds a desired column and has the format
            (column name, data type).

        primary_key : string (default=None)
            Name of the primary key column. If None, no primary key is
            set.

        autoincrement : bool (default=False)
            Set the primary key column to automatically increment.

        verbose : bool (default=False)
            Print out SQL command information.

        Returns
        -------
        Table object

        """

        if isinstance(dtypes, pd.DataFrame):
            ## populate the table with the contents from a dataframe
            idx = dtypes.index
            # figure out the primary key
            if idx.name is not None:
                if primary_key is not None and primary_key != idx.name:
                    raise ValueError("primary key mismatch")
                primary_key = idx.name
            # extract the data and column names
            data = [list(dtypes.as_matrix()[i]) for i in xrange(len(dtypes))]
            names = list(dtypes.columns)
            if primary_key is not None:
                for i in xrange(len(dtypes)):
                    data[i].insert(0, idx[i])
                names.insert(0, primary_key)
            # parse data types
            d = dict(zip(names, data[0]))
            dtypes = dict_to_dtypes(d, order=names)
            # coerce data with the data types we just extracted
            data = [[dtypes[i][1](x[i]) for i in xrange(len(x))] for x in data]
        else:
            data = None

        args = []

        for label, dtype in dtypes:
            # parse the python type into a SQL type
            if dtype is None:
                sqltype = "NULL"
            elif dtype is int or dtype is long:
                sqltype = "INTEGER"
            elif dtype is float:
                sqltype = "REAL"
            elif dtype is str or dtype is unicode:
                sqltype = "TEXT"
            elif dtype is buffer:
                sqltype = "BLOB"
            else:
                raise ValueError("invalid data type: %s" % dtype)

            # construct the SQL syntax for this column
            arg = "%s %s" % (label, sqltype)
            if primary_key is not None and primary_key == label:
                if sqltype != "INTEGER":
                    raise ValueError(
                        "invalid data type for primary key: %s" % dtype)
                arg += " PRIMARY KEY"
                if autoincrement:
                    arg += " AUTOINCREMENT"

            args.append(arg)

        # connect to the database and create the table
        cmd = "CREATE TABLE %s(%s)" % (name, ', '.join(args))
        sql_execute(db, cmd, verbose=verbose)

        # create a Table object
        tbl = cls(db, name, verbose=verbose)

        # insert data, if it was given
        if data is not None:
            tbl.insert(values=data)

        return tbl

    def __init__(self, db, name, verbose=False):
        """Create an interface to the table `name` in the database `db`.

        Parameters
        ----------
        db : (string)
            The path to the SQLite database.

        name : (string)
            The name of the table in the database.

        verbose : bool (default=False)
            Print out SQL command information.

        """

        # save the parameters
        self.db = str(db)
        self.name = str(name)
        self.verbose = bool(verbose)

        # query the database for information about the table
        cmd = ("SELECT sql FROM sqlite_master "
               "WHERE tbl_name='%s' and type='table'" % name)
        info = sql_execute(self.db, cmd, fetchall=True, verbose=verbose)

        # parse the response -- it will look like 'CREATE TABLE
        # name(col1 TYPE, col2 TYPE, ...)'
        args = re.match("([^\(]*)\((.*)\)", info[0][0]).groups()[1]

        # compute repr string
        self.repr = "%s(%s)" % (self.name, args)

        # get the column names
        cols = args.split(", ")
        self.columns = tuple([x.split(" ")[0] for x in cols])

        # parse primary key, if any
        pk = [x.split(" ", 1)[1].find("PRIMARY KEY") > -1 for x in cols]
        primary_key = np.nonzero(pk)[0]
        if len(primary_key) > 1:
            raise ValueError("more than one primary key: %s" % primary_key)
        elif len(primary_key) == 1:
            self.primary_key = self.columns[primary_key[0]]
        else:
            self.primary_key = None

        # parse autoincrement, if applicable
        ai = [x.split(" ", 1)[1].find("AUTOINCREMENT") > -1 for x in cols]
        autoincrement = np.nonzero(ai)[0]
        if len(autoincrement) > 1:
            raise ValueError("more than one autoincrementing "
                             "column: %s" % autoincrement)
        elif self.primary_key is not None and len(autoincrement) == 1:
            if self.primary_key != self.columns[autoincrement[0]]:
                raise ValueError("autoincrement is different from primary key")
            self.autoincrement = True
        else:
            self.autoincrement = False

    def _where(self, args):
        """Helper function to parse a WHERE statement.

        The `args` parameter holds the conditional for the WHERE
        statement. If `args` is a sequence, then the first element is
        the conditional and the second element is an argument or list of
        arguments for that conditional (i.e., where there are ? in the
        conditional).

        self._where("age=25")

        If you need to pass in variable arguments, use question
        marks, e.g.:

        self._where("age=?", 25)
        self._where("age=? OR name=?", (25, "Ben Bitdiddle"))

        Parameters
        ----------
        args : string or (string, value) or (string, (value1, value2, ...))
            Conditional for the WHERE statement (see above).

        Returns
        -------
        2-tuple of (conditional string, argument list)

        """

        # add a selection filter, if specified
        if args is not None:
            if not hasattr(args, '__iter__'):
                args = (args, None)
            where_str, where_args = args
            query = " WHERE %s" % where_str
            if where_args is None:
                out = (query, [])
            else:
                if not hasattr(where_args, "__iter__"):
                    where_args = (where_args,)
                out = (query, where_args)
        else:
            out = ("", [])

        return out

    def drop(self):
        """Drop the table from its database.

        Note that after calling this method, this Table object will no
        longer function properly, as it will correspond to a table that
        does not exist.

        """

        cmd = "DROP TABLE %s" % self.name
        sql_execute(self.db, cmd, verbose=self.verbose)

    def insert(self, values=None):
        """Insert values into the table.

        The `values` parameter should be a list of non-string sequences
        (or a single sequence, which will then be encased in a
        list). Each sequence is handled as follows:

            - If the sequence is a dictionary, then the keys should
              correspond to column names and the values should match the
              data types of the columns.

            - If the sequence is not a dictionary, it should have length
              equal to the number of columns and each element should be
              a value to be inserted in the corresponding column.

        If the table has an autoincrementing primary key, then this
        value should be excluded from every sequence as it will be
        filled in automatically.

        """

        # argument parsing -- `values` should be a list of sequences
        if values is None:
            values = {}
        if hasattr(values, 'keys') or not hasattr(values, "__iter__"):
            values = [values]
        elif not hasattr(values[0], "__iter__"):
            values = [values]

        if not hasattr(values[0], "__iter__"):
            raise ValueError(
                "expected dict or list/tuple, got: %s" % type(values[0]))

        # if we're not trying to insert a value for the primary key,
        # exclude it from the column list
        cols = list(self.columns)
        if len(values[0]) == (len(cols) - 1) and self.primary_key is not None:
            cols.remove(self.primary_key)
        ncol = len(cols)

        # extract the entries from the values that were given
        entries = []
        for vals in values:
            if hasattr(vals, 'keys'):
                entry = tuple([vals.get(key, None) for key in cols])
            elif hasattr(vals, "__iter__"):
                if len(vals) != ncol:
                    raise ValueError("expected %d values, got %d" % (
                        ncol, len(vals)))
                entry = tuple(vals)
            else:
                raise ValueError(
                    "expected dict or list/tuple, got: %s" % type(vals))

            entries.append(entry)

        # target string of NULL and question marks
        qm = ["?"]*ncol
        qm = ", ".join(qm)
        c = ", ".join(cols)

        # perform the insertion
        for entry in entries:
            cmd = ("INSERT INTO %s(%s) VALUES (%s)" % (
                self.name, c, qm), entry)
            sql_execute(self.db, cmd, verbose=self.verbose)

    def select(self, columns=None, where=None):
        """Select data from the table.

        Parameters
        ----------
        columns : (default=None)
            The column names to select. If None, all columns are
            selected. Can be either a single value (string) or a list of
            strings.

        where : (default=None)
            Additional filtering to perform on the data akin to the
            'WHERE' SQL statement, e.g.:

            where="age=25"

            If you need to pass in variable arguments, use question
            marks, e.g.:

            where=("age=?", 25)
            where=("age=? OR name=?", (25, "Ben Bitdiddle"))

        Returns
        -------
        A pandas DataFrame containing the queried data. Column names
        correspond to the table column names, and if there is a primary
        key column, it will be used as the index.

        """

        # argument parsing
        if columns is None:
            cols = list(self.columns)
        else:
            if not hasattr(columns, '__iter__'):
                cols = [columns]
            else:
                cols = list(columns)

        # select primary key even if not given, so we can use the
        # correct index later
        if self.primary_key is not None and self.primary_key not in cols:
            cols.insert(0, self.primary_key)
        sel = ",".join(cols)

        # base query
        query = "SELECT %s FROM %s" % (sel, self.name)
        where_str, where_args = self._where(where)
        query += where_str
        cmd = [query]
        if len(where_args) > 0:
            cmd.append(where_args)

        # connect to the database and execute the query
        rows = sql_execute(self.db, cmd, fetchall=True, verbose=self.verbose)

        # now we need to parse the result into a DataFrame
        if self.primary_key in cols:
            index = self.primary_key
        else:
            index = None
        data = pd.DataFrame.from_records(
            rows, columns=cols, index=index,
            coerce_float=True)

        return data

    def __getitem__(self, key):
        """Select data from the table.

        This method wraps around `self.select` in a few ways.

        1. If a string is given, the column with that name is
        selected. For example:

            table['name']

        2. If a list of strings is given, the columns with those names
        are selected. For example:

            table['name', 'age']

        3. If the table has an autoincrementing primary key, you can use
        integer indexing and slicing syntax to select rows by their
        primary keys. For example:

            table[0]
            table[:5]
            table[7:]

        Returns
        -------
        The output of `self.select` called as described above.

        """

        if isinstance(key, int):
            # select a row
            if self.primary_key is None:
                raise ValueError("no autoincrementing primary key column")
            data = self.select(where=("%s=?" % self.primary_key, key))

        elif isinstance(key, slice):
            # select multiple rows
            if (key.start is None and
                    key.stop is None and
                    key.step in (None, 1)):
                where = None

            else:
                if self.primary_key is None:
                    raise ValueError("no autoincrementing primary key column")
                if key.step not in (None, 1):
                    raise ValueError("cannot handle step size > 1")
                elif key.start is None:
                    where = ("%s<?" % self.primary_key, key.stop)
                elif key.stop is None:
                    where = ("%s>=?" % self.primary_key, key.start)
                else:
                    where = ("%s<? AND %s>=?" % (
                        self.primary_key, self.primary_key),
                        (key.stop, key.start))
            data = self.select(where=where)

        elif isinstance(key, str):
            # select a column
            data = self.select(key)

        elif all(isinstance(k, str) for k in key):
            # select multiple columns
            data = self.select(key)

        else:
            raise ValueError("invalid key: %s" % key)

        return data

    def update(self, values, where=None):
        """Update data in the table.

        Parameters
        ----------
        values : dict
            The column names (keys) and new values to update.

        where : (default=None)
            Additional filtering to perform on the data akin to the
            'WHERE' SQL statement, e.g.:

            where="age=25"

            If you need to pass in variable arguments, use question
            marks, e.g.:

            where=("age=?", 25)
            where=("age=? OR name=?", (25, "Ben Bitdiddle"))

        """

        if not hasattr(values, 'keys'):
            raise ValueError("expected a dictionary, got %s" % type(values))

        # base update
        update = "UPDATE %s SET " % self.name
        update += ", ".join(["%s=?" % key for key in sorted(values.keys())])
        args = [values[key] for key in sorted(values.keys())]

        # filter with WHERE
        where_str, where_args = self._where(where)
        update += where_str
        args.extend(where_args)
        cmd = [update]
        if len(args) > 0:
            cmd.append(args)

        # connect to the database and execute the update
        sql_execute(self.db, cmd, verbose=self.verbose)

    def delete(self, where=None):
        """Delete rows from the table.

        Parameters
        ----------
        where : (default=None)
            Filtering to determine which rows should be deleted, akin to
            the 'WHERE' SQL statement, e.g.:

            where="age=25"

            If you need to pass in variable arguments, use question
            marks, e.g.:

            where=("age=?", 25)
            where=("age=? OR name=?", (25, "Ben Bitdiddle"))

            NOTE: If where is None, then ALL rows will be deleted!

        """

        # base update
        delete = "DELETE FROM %s" % self.name

        # filter with WHERE
        where_str, where_args = self._where(where)
        delete += where_str
        cmd = [delete]
        if len(where_args) > 0:
            cmd.append(where_args)

        # connect to the database and execute the update
        sql_execute(self.db, cmd, verbose=self.verbose)

    def save_csv(self, path, columns=None, where=None):
        """Write table data to a CSV text file.

        Takes in a `path` for the file as well as any arguments and/or
        keyword arguments to be passed to `self.select`. The output of
        `self.select` with those arguments is what will be written to
        the csv file.

        Parameters
        ----------
        path : string
            Path to save the csv file.
        columns : (default=None) See `self.select`
        where : (default=None) See `self.select`

        """

        table = self.select(columns=columns, where=where)
        table.to_csv(path)

    def __repr__(self):
        return self.repr

    def __str__(self):
        return self.repr