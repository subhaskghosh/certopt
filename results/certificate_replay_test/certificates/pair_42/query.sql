SELECT
  person.firstname,
  person.lastname,
  address.city,
  address.state
FROM ADDRESS
RIGHT JOIN PERSON
  ON address.personid = person.personid