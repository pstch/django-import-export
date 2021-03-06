from __future__ import unicode_literals

import functools
from copy import deepcopy
from warnings import warn
import sys
import traceback

import tablib
from diff_match_patch import diff_match_patch

from django.utils.safestring import mark_safe
from django.utils.datastructures import SortedDict
from django.utils import six
from django.db import transaction
from django.db.models.related import RelatedObject
from django.conf import settings

from .results import Error, Result, RowResult
from .fields import Field
from import_export import widgets
from .instance_loaders import (
    ModelInstanceLoader,
)


try:
    from django.utils.encoding import force_text
except ImportError:
    from django.utils.encoding import force_unicode as force_text


USE_TRANSACTIONS = getattr(settings, 'IMPORT_EXPORT_USE_TRANSACTIONS', False)

M2M_FIELDS = ('ManyToManyField', )
FK_FIELDS = ('ForeignKey', 'OneToOneField')
DECIMAL_FIELDS = ('DecimalField', )
DATETIME_FIELDS = ('DateTimeField', )
DATE_FIELDS = ('DateField', )
INTEGER_FIELDS = ('IntegerField', 'PositiveIntegerField',
                  'PositiveSmallIntegerField', 'SmallIntegerField',
                  'AutoField')
BOOLEAN_FIELDS = ('BooleanField', 'BooleanWidget')

FIELD_WIDGET_MAPPINGS = {
    M2M_FIELDS: lambda f: functools.partial(widgets.ManyToManyWidget,
                                            model=f.rel.to),
    FK_FIELDS: lambda f: functools.partial(widgets.ForeignKeyWidget,
                                           model=f.rel.to),
    DECIMAL_FIELDS: widgets.DecimalWidget,
    DATETIME_FIELDS: widgets.DateTimeWidget,
    DATE_FIELDS: widgets.DateWidget,
    INTEGER_FIELDS: widgets.IntegerWidget,
    BOOLEAN_FIELDS: widgets.BooleanWidget
}


def _field_name_follows_rel(name):
    """Used to know if a field name follows a relationship (contains '__')

    """
    return '__' in name


def _get_field_by_name(field_name, model):
    """Uses a Django internal function get a field by its name

    See http://bit.ly/1iLX0BY
    """
    return model._meta.get_field_by_name(field_name)[0]


def _get_relationship_target_field(field_name, model):
    """Parses a field name, for a field that spans relationships, to get the
    last field. Takes a field name, and the corresponding model, as
    argument.

    """
    rels = field_name.split('__')

    for rel in rels[:-1]:
        # for each rel in rels that is not the last,
        # replace model with the relationship target
        model = _get_field_by_name(rel, model).rel.to
    last = _get_field_by_name(rels[-1], model)
    return last if not isinstance(last, RelatedObject) else last.field


class ResourceOptions(object):
    """The inner Meta class allows for class-level configuration of how the
    Resource should behave. The following options are available:

    * ``fields`` - Controls what introspected fields the Resource
      should include. A whitelist of fields.

    * ``exclude`` - Controls what introspected fields the Resource should
      NOT include. A blacklist of fields.

    * ``model`` - Django Model class. It is used to introspect available
      fields.

    * ``instance_loader_class`` - Controls which class instance will take
      care of loading existing objects.

    * ``import_id_fields`` - Controls which object fields will be used to
      identify existing instances.

    * ``column_order`` - Controls order for columns.

    * ``widgets`` - dictionary defines widget kwargs for fields.

    * ``use_transactions`` - Controls if import should use database
      transactions. Default value is ``None`` meaning
      ``settings.IMPORT_EXPORT_USE_TRANSACTIONS`` will be evaluated.

    * ``skip_unchanged`` - Controls if the import should skip
      unchanged records.  Default value is False

    * ``report_skipped`` - Controls if the result reports skipped rows
      Default value is True

    """
    fields = None
    model = None
    exclude = None
    instance_loader_class = None
    import_id_fields = ['id']
    column_order = None
    widgets = None
    use_transactions = None
    skip_unchanged = False
    report_skipped = True

    def __new__(cls, meta=None):
        overrides = {}

        if meta:
            for override_name in dir(meta):
                if not override_name.startswith('_'):
                    overrides[override_name] = getattr(meta, override_name)

        return object.__new__(type(str('ResourceOptions'), (cls,), overrides))


class DeclarativeMetaclass(type):

    def __new__(cls, name, bases, attrs):
        _field_list = []

        # Move the fields (Resource attributes) to the ``fields`` attribute

        # FIXME: not sure that .copy() is needed in Py3K
        for field_name, obj in attrs.copy().items():
            if isinstance(obj, Field):
                field = attrs.pop(field_name)
                if not field.column_name:
                    field.column_name = field_name
                _field_list.append((field_name, field))

        attrs['fields'] = SortedDict(_field_list)
        del _field_list

        new_class = super(DeclarativeMetaclass, cls).__new__(
            cls, name,
            bases, attrs
        )
        opts = getattr(new_class, 'Meta', None)
        new_class._meta = ResourceOptions(opts)

        return new_class


class Resource(six.with_metaclass(DeclarativeMetaclass)):
    """
    Resource defines how objects are mapped to their import and export
    representations and handle importing and exporting data.
    """

    def get_use_transactions(self):
        """
        #TODO: Add docstring
        """
        if self._meta.use_transactions is None:
            return USE_TRANSACTIONS
        else:
            return self._meta.use_transactions

    def get_fields(self):
        """
        Returns fields in ``column_order`` order.
        """
        return [self.fields[f] for f in self.get_column_order()]

    @classmethod
    def get_field_name(cls, field):
        """
        Returns field name for given field.
        """
        for field_name, f in cls.fields.items():
            if f == field:
                return field_name
        raise AttributeError("Field %s does not exists in %s resource" % (
            field, cls))

    def init_instance(self, row=None):
        """
        #TODO: Add docstring
        """
        raise NotImplementedError()

    def get_instance(self, instance_loader, row):
        """
        #TODO: Add docstring
        """
        return instance_loader.get_instance(row)

    def get_or_init_instance(self, instance_loader, row):
        """
        #TODO: Add docstring
        """
        instance = self.get_instance(instance_loader, row)
        if instance:
            return (instance, False)
        else:
            return (self.init_instance(row), True)

    def save_instance(self, instance, dry_run=False):
        """
        #TODO: Add docstring
        """
        self.before_save_instance(instance, dry_run)
        if not dry_run:
            instance.save()
        self.after_save_instance(instance, dry_run)

    def before_save_instance(self, instance, dry_run):
        """
        Override to add additional logic.
        """
        pass

    def after_save_instance(self, instance, dry_run):
        """
        Override to add additional logic.
        """
        pass

    def delete_instance(self, instance, dry_run=False):
        """
        #TODO: Add docstring
        """
        self.before_delete_instance(instance, dry_run)
        if not dry_run:
            instance.delete()
        self.after_delete_instance(instance, dry_run)

    def before_delete_instance(self, instance, dry_run):
        """
        Override to add additional logic.
        """
        pass

    def after_delete_instance(self, instance, dry_run):
        """
        Override to add additional logic.
        """
        pass

    def import_field(self, field, obj, data):
        """
        #TODO: Add docstring
        """
        if field.attribute and field.column_name in data:
            field.save(obj, data)

    def import_obj(self, obj, data, dry_run):
        """
        #TODO: Add docstring
        """
        [
            self.import_field(field, obj, data)
            for field in self.get_fields()
            if not isinstance(field.widget, widgets.ManyToManyWidget)
        ]

    def save_m2m(self, obj, data, dry_run):
        """
        Saves m2m fields.

        Model instance need to have a primary key value before
        a many-to-many relationship can be used.
        """
        if not dry_run:
            [
                self.import_field(field, obj, data)
                for field in self.get_fields()
                if isinstance(field.widget, widgets.ManyToManyWidget)
            ]

    def for_delete(self, row, instance):
        """
        Returns ``True`` if ``row`` importing should delete instance.

        Default implementation returns ``False``.
        Override this method to handle deletion.
        """
        return False

    def skip_row(self, instance, original):
        """Returns ``True`` if ``row`` importing should be skipped.

        Default implementation returns ``False`` unless skip_unchanged
        == True.  Override this method to handle skipping rows meeting
        certain conditions.

        """
        def get_related_objects(obj):
            return list(field.get_value(obj).all())
        if not self._meta.skip_unchanged:
            return False
        for field in self.get_fields():
            try:
                # For any ManyRelatedManager field
                # we need to compare the results
                if get_related_objects(instance) != \
                   get_related_objects(original):
                    return False
            except AttributeError:
                if field.get_value(instance) != field.get_value(original):
                    return False
        return True

    def get_field_diff(self, dmp_instance,
                       field, original, current,
                       dry_run=False):
        """
        ``dry_run`` allows handling special cases when object is not saved
        to database (ie. m2m relationships).
        """
        original = self.export_field(field, original) if original else ""
        current = self.export_field(field, current) if current else ""

        diff = dmp_instance.diff_main(
            force_text(original),
            force_text(current)
        )
        # we assume the field contains human-readable content (see
        # diff_match_patch docs)
        dmp_instance.diff_cleanupSemantic(diff)

        # TODO: implement own diff_prettyHtml : "This function is
        # mainly intended as an example from which to write ones own
        # display functions."
        # https://code.google.com/p/google-diff-match-patch/wiki/API
        return dmp_instance.diff_prettyHtml(diff)

    def get_diff(self, original, current, dry_run=False):
        """
        Get diff between original and current object when ``import_data``
        is run.

        ``dry_run`` allows handling special cases when object is not saved
        to database (ie. m2m relationships).
        """
        # https://code.google.com/p/google-diff-match-patch/wiki/API
        dmp_instance = diff_match_patch()

        return [
            # we have to use mark_safe is the diff contains HTML
            # markup (which is bad)
            mark_safe(self.get_field_diff(
                dmp_instance,
                field,
                original,
                current,
                dry_run
            )) for field in self.get_fields()
        ]

    def get_diff_headers(self):
        """
        Diff representation headers.
        """
        return self.get_column_headers()

    def before_import(self, dataset, dry_run):
        """
        Override to add additional logic.
        """
        pass

    def import_data(self,
                    dataset, dry_run=False,
                    raise_errors=False, use_transactions=None):
        """
        Imports data from ``dataset``.

        ``use_transactions``
            If ``True`` import process will be processed inside transaction.
            If ``dry_run`` is set, or error occurs, transaction will be rolled
            back.
        """
        result = Result()

        if use_transactions is None:
            use_transactions = self.get_use_transactions()

        if use_transactions is True:
            # when transactions are used we want to create/update/delete object
            # as transaction will be rolled back if dry_run is set
            real_dry_run = False
            transaction.enter_transaction_management()
            transaction.managed(True)
        else:
            real_dry_run = dry_run

        instance_loader = self._meta.instance_loader_class(self, dataset)

        try:
            self.before_import(dataset, real_dry_run)
        except Exception as e:
            tb_info = traceback.format_exc(sys.exc_info()[2])
            result.base_errors.append(Error(repr(e), tb_info))
            if raise_errors:
                if use_transactions:
                    transaction.rollback()
                    transaction.leave_transaction_management()
                raise

        for row in dataset.dict:
            try:
                row_result = RowResult()
                instance, new = self.get_or_init_instance(instance_loader, row)
                if new:
                    row_result.import_type = RowResult.IMPORT_TYPE_NEW
                else:
                    row_result.import_type = RowResult.IMPORT_TYPE_UPDATE
                row_result.new_record = new
                original = deepcopy(instance)
                if self.for_delete(row, instance):
                    if new:
                        row_result.import_type = RowResult.IMPORT_TYPE_SKIP
                        row_result.diff = self.get_diff(
                            None,
                            None,
                            real_dry_run
                        )
                    else:
                        row_result.import_type = RowResult.IMPORT_TYPE_DELETE
                        self.delete_instance(instance, real_dry_run)
                        row_result.diff = self.get_diff(
                            original,
                            None,
                            real_dry_run
                        )
                else:
                    self.import_obj(instance, row, real_dry_run)
                    if self.skip_row(instance, original):
                        row_result.import_type = RowResult.IMPORT_TYPE_SKIP
                    else:
                        self.save_instance(instance, real_dry_run)
                        self.save_m2m(instance, row, real_dry_run)
                        # Add object info to RowResult for LogEntry
                        row_result.object_repr = str(instance)
                        row_result.object_id = instance.pk
                    row_result.diff = self.get_diff(
                        original,
                        instance,
                        real_dry_run
                    )
            except Exception as e:
                tb_info = traceback.format_exc(2)
                row_result.errors.append(Error(e, tb_info))
                if raise_errors:
                    if use_transactions:
                        transaction.rollback()
                        transaction.leave_transaction_management()
                    six.reraise(*sys.exc_info())

            if row_result.import_type is not RowResult.IMPORT_TYPE_SKIP or \
               self._meta.report_skipped:
                result.rows.append(row_result)

        if use_transactions:
            if dry_run or result.has_errors():
                transaction.rollback()
            else:
                transaction.commit()
            transaction.leave_transaction_management()

        return result

    def get_column_order(self):
        # TODO: Docstring
        return self._meta.column_order or self.fields.keys()

    def get_export_order(self):
        # TODO: Docstring
        warn("get_export_order() is deprecated, please use get_column_order()",
             DeprecationWarning)
        return self.get_column_order()

    def export_field(self, field, obj):
        # TODO: Docstring
        field_name = self.get_field_name(field)
        # TOFIND: WTF is dehydrate ?
        method = getattr(self, 'dehydrate_%s' % field_name, None)
        if method is not None:
            return method(obj)
        return field.export(obj)

    def export_instance(self, obj):
        # TODO: Docstring
        return [self.export_field(field, obj) for field in self.get_fields()]

    def export_resource(self, obj):
        # TODO: Docstring
        warn("export_resource() is deprecated, please use export_instance()",
             DeprecationWarning)
        return self.export_instance(obj)

    def get_column_headers(self):
        # TODO: Docstring
        return [force_text(field.column_name) for field in self.get_fields()]

    def get_export_headers(self):
        # TODO: Docstring
        warn("get_export_headers() is deprecated, please "
            " use get_column_headers()",
             DeprecationWarning)
        return self.get_column_headers()

    def export(self, queryset=None):
        """Exports a resource. Can take a queryset argument to export
        specifically that queryset.

        """
        # TODO: Shouldn't that be done OUTSIDE the Resource ?
        if queryset is None:
            # no explicit queryset, get the queryset for all objects
            queryset = self.get_queryset()
        data = tablib.Dataset(headers=self.get_column_headers())
        # Iterate without the queryset cache, to avoid wasting memory when
        # exporting large datasets.
        for obj in queryset.iterator():
            data.append(self.export_instance(obj))
        return data


class ModelDeclarativeMetaclass(DeclarativeMetaclass):
    """#TODO: Add docstring"""
    def __new__(cls, name, bases, attrs):
        """#TODO: Add docstring"""
        new_class = super(ModelDeclarativeMetaclass,
                          cls).__new__(cls, name, bases, attrs)

        def parse_field(field, field_name=None):
            """Instantiate an import field with a widget and a Django field. Takes a
            Django field and its field_name as argument

            Why is field_name passed as an argument ? Because in some
            cases (fields spanning relationship, for example),
            field_name is different from field.name (in that case,
            contains the full path to the Django field)"""
            if field_name is None:
                field_name = field.name
            # ModelResources provide a function to get an
            # import widget for each Django field, that we
            # initialize and use to instantiate an import
            # field, appended to the temp field list and set
            # on ModelResource._meta.fields
            widget_class = new_class.widget_from_django_field(field)
            widget_kwargs = new_class.widget_kwargs_for_field(field_name)

            return Field(
                attribute=field_name,
                column_name=field_name,
                widget=widget_class(**widget_kwargs)
            )

        # TOFIND: What is opts ? ResourceOption ?
        opts = new_class._meta

        if not opts.instance_loader_class:
            opts.instance_loader_class = ModelInstanceLoader

        if opts.model:
            model_opts = opts.model._meta

            # Update new ModelResource with fields from Django
            # model's metaclass (_meta.fields and _meta.many_to_many)
            new_class.fields.update(SortedDict((
                (
                    # 1st element of item tuples: field name
                    field.name,
                    # 2nd element of item tuples : import
                    # field, from the Django field
                    parse_field(field)
                )
                for field in sorted(
                        model_opts.fields +
                        model_opts.many_to_many
                    )
                if
                (
                    # check that current field is not
                    # present in ModelResource fields (if
                    # defined)
                    opts.fields is None or
                        field.name in opts.fields
                ) and (
                    # check that current field isn't
                    # excluded by the ModelResource
                    opts.exclude is None or
                    field.name not in opts.exclude
                ) and (
                    # check that current field isn't
                    # already defined in the new
                    # ModelResource, by
                    # DeclarativeMetaclass for example
                    field.name not in new_class.fields
                )
            )))
            # Update new ModelResource with relationship-spanning
            # fields defined in ModelResource Meta options.
            #
            # Will not override fields that we got from the model
            # metaclass (model_opts.fields and model_opts.many_to_many).
            if opts.fields is not None:
                # temporary list to store Field objects, before sorting
                # them and setting them on new_class
                new_class.fields.update(SortedDict((
                    (
                        # 1st element of item tuples : field name
                        field_name,
                        # 2nd element of item tuples : import
                        # field, from the last relationship in the
                        # relationship-spanning field name
                        parse_field(
                            _get_relationship_target_field(
                                field_name,
                                opts.model
                            ),
                            field_name
                        )
                    )
                    # iterate through ModelResource metaclass fields
                    for field_name in opts.fields if
                    # check that field name is not defined
                    # either by the Model's metaclass or
                    # as attribute/property in the ModelResource
                    # metaclass
                    field_name not in new_class.fields and
                    # check that the field name actually spans
                    # relationships
                    _field_name_follows_rel(field_name)
                )))

        return new_class


class ModelResource(six.with_metaclass(ModelDeclarativeMetaclass, Resource)):
    """ModelResource is Resource subclass for handling Django models.

    It allows us to get the corresponding Widget for Django fields,
retrieves the data defined in the Resource Meta class
(ResourceOptions), provides the model queryset and a function to
initialize a model instance.

    """

    @classmethod
    def widget_from_django_field(cls, f, default=widgets.Widget):
        """
        Returns the widget that would likely be associated with each
        Django type, accordingly to FIELD_WIDGET_MAPPINGS
        """
        internal_type = f.get_internal_type()

        # FIELD_WIDGET_MAPPINGS is a dictionary contaning a tuple of
        # internal types as key, and the corresponding widget (or
        # lambda returning widget, taking field as argument) as values
        for internal_types, widget in FIELD_WIDGET_MAPPINGS.items():
            if internal_type in internal_types:
                if isinstance(widget, type) and \
                   issubclass(widget, widgets.Widget):
                    # Not a lambda function, return directly
                    return widget
                else:
                    # Lambda function, call with field as arg. Needed
                    # for widget such as ForeignKeyWidget or
                    # ManyToManyWidget
                    return widget(f)

        return default

    @classmethod
    def widget_kwargs_for_field(self, field_name):
        """Returns widget kwargs (defined in Meta options) for given
        field_name.

        """
        if self._meta.widgets:
            return self._meta.widgets.get(field_name, {})
        return {}

    def get_import_id_fields(self):
        """Returns import identification fields (defined in Meta options)

        """
        return self._meta.import_id_fields

    def get_queryset(self):
        """Returns model queryset (model defined in Meta options)

        """
        return self._meta.model.objects.all()

    def init_instance(self, row=None):
        """Initialize a model instance (model defined in Meta options)

        """
        return self._meta.model()


def modelresource_factory(model, resource_class=ModelResource):
    """Factory for creating ``ModelResource`` class for given Django
    model.

    """
    resource_metaclass = type(
        str('Meta'),
        (object,),
        {'model': model}
    )
    class_name = "%s%s" % (model.__name__, str('Resource'))

    return ModelDeclarativeMetaclass(
        class_name,
        (resource_class,),
        {'Meta': resource_metaclass}
    )
